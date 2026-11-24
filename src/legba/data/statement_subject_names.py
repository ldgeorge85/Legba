# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The names a SOURCE would plausibly use for a statement's subject — gate
G2's subject check reads them (plan P1 §3 G2: "the subject's name or an alias
is in a quote or the signal title").

WHY. The S-3 dry run checked subjects against target DISPLAY names ("Watch —
Iran, Islamic Republic of"), words no source writes; ``entity_aliases`` is
empty. This module collects what the instance ALREADY declares about how a
subject is named, each name with where it came from, and never guesses:

==================  ===========================================================
kind                where it comes from
==================  ===========================================================
``label``           the profile's declared subject label, tier stripped
                    (``Watch — Pakistan`` → ``Pakistan``), and an ISO-style
                    comma name turned the right way round (``Iran, Islamic
                    Republic of`` → ``Islamic Republic of Iran``)
``official``        pycountry's name / official name / common name for the
                    subject's single declared geo code (the gazetteer's
                    declared polity universe)
``short``           the gazetteer's names for that code: ``target_slug_names``,
                    ``desk_countries``, ``slug_to_country``,
                    ``iso2_home_aliases``, the ``officeholder_country_groups``
                    group holding one of them, ``alias_map`` keys onto them
``abbreviation``    ``slug_to_abbrev`` and any declared form with no lower-case
                    letter (``UK``, ``DPRK``) — matched CASE-SENSITIVELY, the
                    gazetteer's house rule (``US`` is a country, ``us`` a
                    pronoun)
``demonym``         ``demonym_map`` keys onto the subject, ``irregular_demonyms``
``capital``         a capital-as-government metonym — ONLY a form the profile's
                    ``subject_homonyms.<code>.subject`` already declares for
                    this subject AND the fact extractor's curated
                    seat-of-government list already knows (``Pyongyang`` for
                    North Korea, ``London`` for the United Kingdom)
``entity``          surface forms an entity row already carries (the merge
                    history ``data.merged_aliases`` of ``entity_profiles``),
                    handed in by the caller and cleaned here (markup,
                    possessives, leading articles dropped); for an entity
                    SUBJECT keyed by its profile id (S-5) the profile's
                    canonical name and aliases, a parenthesised acronym
                    (``… (ZNPP)``) split out as an abbreviation
==================  ===========================================================

A form with no capital letter and no digit (``government``, ``the army``) is
not a proper name: it is kept with ``usable=False`` (kind ``entity_generic``)
— under the case-folded match it would be "found" in every quote.

Forms a profile declares as naming the subject but that are none of these (a
leader's surname, a state company, a news agency — ``Starmer``, ``Aramco``,
``KCNA``) are returned with ``usable=False``: a sentence about the UK is not
supported by a quote that only names its prime minister. Declared homonym
EXCLUSIONS (``subject_homonyms.<code>.exclude``: ``KP`` in the Pakistani press
is Khyber Pakhtunkhwa, ``Niger Delta`` is in Nigeria) are carried too: a name
match inside an excluded form's occurrence does not count.

A thematic subject with several declared geo codes (a sea lane, a flow) or
none (a region, a situation) is named by its own label only; the countries it
spans are separate subjects (:func:`related_subjects`) the typing seat may be
offered — a sentence about Iran on the Hormuz desk is about Iran.

PURE. The defaults read the packaged profile exports through their leaf
loaders (``legba.data.subject``, ``legba.data.gazetteer``); every input can
be injected. No model, no table.
"""
from __future__ import annotations

import html
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .provenance.text_fold import normalize_for_match
from .statement_content import SHORT_NAME_CHARS

#: Name kinds a source would plausibly use for the subject (usable for G2).
USABLE_KINDS = frozenset({"label", "official", "short", "abbreviation", "demonym",
                          "capital", "entity"})

_ZERO_WIDTH_RE = re.compile("[​‌‍⁠﻿­]")
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
_POSSESSIVE_RE = re.compile(r"\s*['’]\s*s$", re.IGNORECASE)


@dataclass(frozen=True)
class SubjectName:
    """One name for a subject: the form, its kind (table above), where it
    came from (``gazetteer:<section>``, ``pycountry:<field>``,
    ``profile:label``, ``entity:merged_aliases``), and whether G2 may use it."""

    name: str
    kind: str
    source: str
    usable: bool = True

    @property
    def case_sensitive(self) -> bool:
        """A form with no lower-case letter matches case-sensitively."""
        return not any(ch.islower() for ch in self.name)


@dataclass(frozen=True)
class SubjectNames:
    """Every name collected for one subject, plus the declared exclusions."""

    subject_kind: str
    subject_key: str
    names: tuple[SubjectName, ...] = ()
    excluded: tuple[str, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> tuple[SubjectName, ...]:
        return tuple(n for n in self.names if n.usable)

    def __iter__(self):  # G2 takes an iterable of names
        return iter(self.usable)


# ---------------------------------------------------------------------------
# Matching (case rule + exclusions)
# ---------------------------------------------------------------------------


def _fold_spans(name: str, text: str, *, case_sensitive: bool) -> list[tuple[int, int]]:
    """Word-start spans of ``name`` in ``text`` under the content readers'
    rule (a short name is a whole word; a longer one may carry a suffix)."""
    if case_sensitive:
        n, t = name.strip(), text
    else:
        n, t = normalize_for_match(name), normalize_for_match(text)
    if not n or not t:
        return []
    tail = r"(?!\w)" if len(n) < SHORT_NAME_CHARS or case_sensitive else ""
    lead = r"(?<![\w.])" if case_sensitive else r"(?<!\w)"
    return [m.span() for m in re.finditer(rf"{lead}{re.escape(n)}{tail}", t)]


def name_in_text(name: SubjectName | str, text: str | None, *, excluded: Iterable[str] = ()) -> bool:
    """Is ``name`` stated in ``text``, outside every occurrence of an excluded
    form? An all-capitals form matches case-sensitively (``US`` ≠ ``us``)."""
    if not text:
        return False
    sn = name if isinstance(name, SubjectName) else SubjectName(str(name), "given", "caller")
    exact = sn.case_sensitive
    hits = _fold_spans(sn.name, text, case_sensitive=exact)
    if not hits:
        return False
    blocked: list[tuple[int, int]] = []
    for ex in excluded:
        ex_exact = not any(ch.islower() for ch in ex)
        if ex_exact == exact:
            blocked.extend(_fold_spans(ex, text, case_sensitive=exact))
        elif not exact:
            # an exact exclusion, a folded name: fold the exclusion's hits too
            blocked.extend(_fold_spans(ex, text, case_sensitive=False))
    return any(all(h[1] <= a or h[0] >= b for a, b in blocked) for h in hits)


# ---------------------------------------------------------------------------
# Defaults (the packaged exports, read through their leaf loaders)
# ---------------------------------------------------------------------------


def _default_subjects() -> Mapping[str, Any]:
    from .subject import current

    return {k: {"name": d.name, "geo": list(d.geo), "domain": d.domain}
            for k, d in current().subjects.items()}


def _default_gazetteer() -> Mapping[str, Any]:
    from .gazetteer import gazetteer

    gz = gazetteer()
    return {name: _plain(gz.get(name)) for name in gz.declared}


def _plain(value: Any) -> Any:
    """A loader value as plain mappings/sequences (pydantic sections dumped)."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _default_label_separators() -> tuple[str, ...]:
    from .subject import label_style

    style = label_style()
    return (style.separator, *style.separator_aliases)


def default_metonyms() -> frozenset[str]:
    """The fact extractor's curated seat-of-government list (lower-cased)."""
    from .filters.fact_extractor import _GOVERNMENT_METONYM_SUBJECTS

    return frozenset(_GOVERNMENT_METONYM_SUBJECTS)


# ---------------------------------------------------------------------------
# Collecting names
# ---------------------------------------------------------------------------


def clean_surface_form(raw: str) -> str | None:
    """A stored surface form as a name a source writes, or ``None``: markup
    unescaped, zero-width characters dropped, a possessive and a leading
    article stripped; forms that still carry markup are refused."""
    s = _ZERO_WIDTH_RE.sub("", html.unescape(str(raw or ""))).replace(" ", " ")
    s = re.sub(r"\s+", " ", s).strip(" \t-–—,;:")
    if not s or any(ch in s for ch in "<>&;/\\\"") or "&#" in s:
        return None
    s = _POSSESSIVE_RE.sub("", s)
    s = _LEADING_ARTICLE_RE.sub("", s).strip()
    if len(s) < 2 or not any(ch.isalpha() for ch in s):
        return None
    return s


_PAREN_TAIL_RE = re.compile(r"^(?P<base>[^()]*?)\s*\((?P<inner>[^()]*)\)?\s*$")


def entity_name_forms(raw: str) -> list[tuple[str, str]]:
    """The cleaned forms one stored entity name yields, as ``(form, kind)``:
    the name itself (kind ``entity``), and for ``Base (ACRO)`` — even with the
    closing parenthesis lost, as stored names sometimes are — the base and the
    acronym (kind ``abbreviation``, case-sensitive) separately."""
    cleaned = clean_surface_form(raw)
    if not cleaned:
        return []
    m = _PAREN_TAIL_RE.match(cleaned)
    if not m or "(" not in cleaned:
        return [(cleaned, "entity")]
    out: list[tuple[str, str]] = []
    base = clean_surface_form(m.group("base") or "")
    if base:
        out.append((base, "entity"))
    inner = (m.group("inner") or "").strip()
    if re.fullmatch(r"[A-Z][A-Z0-9&.-]{1,9}", inner):
        out.append((inner, "abbreviation"))
    elif inner and cleaned.endswith(")"):
        out.append((cleaned, "entity"))
    return out


def is_proper_form(name: str) -> bool:
    """A proper name carries a capital letter or a digit somewhere."""
    return any(ch.isupper() or ch.isdigit() for ch in name)


def label_name(label: str, separators: Sequence[str] = ("—", "–", "-")) -> str:
    """The name part of a ``Tier — Name`` label (the first spaced separator
    splits; ``US–Iran War`` keeps its unspaced dash)."""
    for sep in separators:
        token = f" {sep} "
        if token in label:
            return label.split(token, 1)[1].strip()
    return label.strip()


def uncomma(name: str) -> str | None:
    """``Iran, Islamic Republic of`` → ``Islamic Republic of Iran``;
    ``Congo, The Democratic Republic of the`` → ``Democratic Republic of the
    Congo``. ``None`` when the name is not in that ISO comma form."""
    head, sep, tail = name.partition(", ")
    if not sep or not re.search(r"\b(?:of|of the)$", tail.strip(), re.IGNORECASE):
        return None
    out = f"{tail.strip()} {head.strip()}"
    return _LEADING_ARTICLE_RE.sub("", out).strip()


def _strip_parenthetical(name: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*", " ", name).strip()


class _Collector:
    def __init__(self, excluded: Iterable[str]) -> None:
        self.names: list[SubjectName] = []
        self._seen: dict[str, int] = {}
        self._excluded = {normalize_for_match(e) for e in excluded}

    def add(self, name: Any, kind: str, source: str, *, usable: bool = True) -> None:
        text = str(name or "").strip()
        if not text:
            return
        key = text if not any(ch.islower() for ch in text) else normalize_for_match(text)
        if normalize_for_match(text) in self._excluded and not any(ch.islower() for ch in text):
            return  # a declared homonym form is never a name for the subject
        if key in self._seen:
            return
        self._seen[key] = len(self.names)
        self.names.append(SubjectName(text, kind, source, usable))


def _spell(name: str) -> str:
    """A lower-cased gazetteer entry of at most three letters (``uk``,
    ``u.s.``, ``drc``) is an abbreviation: spelled in capitals, so it matches
    case-sensitively (``us`` the pronoun is not ``US``)."""
    letters = re.sub(r"[^A-Za-z]", "", name)
    return name.upper() if 0 < len(letters) <= 3 and " " not in name.strip() else name


def _kind_of_alias(name: str, demonyms: Mapping[str, Any]) -> str:
    if not any(ch.islower() for ch in name):
        return "abbreviation"
    if normalize_for_match(name) in {normalize_for_match(k) for k in demonyms}:
        return "demonym"
    return "short"


def country_names(
    iso2: str,
    *,
    gazetteer: Mapping[str, Any],
    metonyms: Iterable[str] = (),
    collector: _Collector | None = None,
) -> _Collector:
    """Every declared name for one country code, into ``collector``."""
    code = iso2.strip().lower()
    homonyms = (gazetteer.get("subject_homonyms") or {}).get(code) or {}
    out = collector or _Collector(homonyms.get("exclude") or ())
    try:
        import pycountry

        rec = pycountry.countries.get(alpha_2=code.upper())
    except Exception:  # noqa: BLE001 - pycountry absent: the gazetteer still names it
        rec = None
    if rec is not None:
        for attr in ("common_name", "name", "official_name"):
            value = getattr(rec, attr, None)
            if value:
                out.add(uncomma(value) or value, "official", f"pycountry:{attr}")
    demonyms = gazetteer.get("demonym_map") or {}
    canon: set[str] = set()
    for section in ("target_slug_names", "desk_countries", "slug_to_country"):
        for n in (gazetteer.get(section) or {}).get(code) or ():
            out.add(_spell(n), _kind_of_alias(_spell(n), demonyms), f"gazetteer:{section}")
            canon.add(normalize_for_match(n))
    for n in (gazetteer.get("iso2_home_aliases") or {}).get(code.upper()) or ():
        out.add(_spell(n), _kind_of_alias(_spell(n), demonyms), "gazetteer:iso2_home_aliases")
        canon.add(normalize_for_match(n))
    for n in (gazetteer.get("slug_to_abbrev") or {}).get(code) or ():
        out.add(n, "abbreviation", "gazetteer:slug_to_abbrev")
    if rec is not None:
        canon.update(normalize_for_match(getattr(rec, a, "") or "") for a in ("name", "common_name"))
    canon.discard("")
    for group in gazetteer.get("officeholder_country_groups") or ():
        if any(normalize_for_match(g) in canon for g in group):
            for n in group:
                out.add(_spell(n), _kind_of_alias(_spell(n), demonyms),
                        "gazetteer:officeholder_country_groups")
                canon.add(normalize_for_match(n))
    alias_map = gazetteer.get("alias_map") or {}
    targets = {normalize_for_match(v) for k, v in alias_map.items() if normalize_for_match(k) in canon}
    canon |= targets
    for k, v in alias_map.items():
        if normalize_for_match(v) in canon:
            out.add(_spell(k), _kind_of_alias(_spell(k), demonyms), "gazetteer:alias_map")
    for dem, country in demonyms.items():
        if normalize_for_match(country) in canon:
            out.add(dem, "demonym", "gazetteer:demonym_map")
    for country, dems in (gazetteer.get("irregular_demonyms") or {}).items():
        if normalize_for_match(country) in canon:
            for d in dems:
                out.add(d, "demonym", "gazetteer:irregular_demonyms")
    seats = {normalize_for_match(m) for m in metonyms}
    for form in homonyms.get("subject") or ():
        folded = normalize_for_match(form)
        if folded in seats:
            out.add(form, "capital", "gazetteer:subject_homonyms+fact_extractor:metonyms")
        elif folded in canon or folded in {normalize_for_match(k) for k in demonyms if normalize_for_match(demonyms[k]) in canon}:
            out.add(form, _kind_of_alias(form, demonyms), "gazetteer:subject_homonyms")
        else:
            # a leader, a company, an agency: declared as naming the subject in
            # a headline, but a quote naming only it does not name the country
            out.add(form, "declared_other", "gazetteer:subject_homonyms", usable=False)
    return out


def resolve_subject_names(
    subject_kind: str,
    subject_key: str,
    *,
    subjects: Mapping[str, Any] | None = None,
    gazetteer: Mapping[str, Any] | None = None,
    metonyms: Iterable[str] | None = None,
    entity_forms: Mapping[str, Sequence[str]] | None = None,
    separators: Sequence[str] | None = None,
    entity_profiles: Mapping[str, Mapping[str, Any]] | None = None,
) -> SubjectNames:
    """The names a source would plausibly use for (``subject_kind``,
    ``subject_key``), each with its provenance.

    * ``target`` — the profile's declared subject (``subjects``: id →
      ``{name, geo, domain}``): its label name; with ONE declared geo code,
      that country's names too.
    * ``place`` — ``subject_key`` is a country code: that country's names.
    * ``entity`` — ``subject_key`` is the entity's name (entities are keyed
      by name): the name, its cleaned stored forms (``entity_forms``), and the
      country names when the name is a declared country name. When
      ``subject_key`` is a key of ``entity_profiles`` (S-5: an entity subject
      keyed by its profile id; the value carries ``canonical_name`` and
      ``aliases``), the names are the profile's canonical name and aliases,
      cleaned (:func:`entity_name_forms`); a form that is not a proper name
      is kept unusable (``entity_generic``).
    * anything else — the key itself, nothing more.
    """
    subjects = _default_subjects() if subjects is None else subjects
    gazetteer = _default_gazetteer() if gazetteer is None else gazetteer
    seps = _default_label_separators() if separators is None else tuple(separators)
    seats = default_metonyms() if metonyms is None else frozenset(metonyms)
    forms = entity_forms or {}
    kind = str(subject_kind or "")
    key = str(subject_key or "").strip()
    geo: tuple[str, ...] = ()
    detail: dict[str, Any] = {}
    label_forms: list[str] = []
    if kind == "target":
        decl = subjects.get(key) or {}
        geo = tuple(str(g).upper() for g in (decl.get("geo") or ()))
        if decl.get("name"):
            name = _strip_parenthetical(label_name(str(decl["name"]), seps))
            inv = uncomma(name)
            label_forms.append(inv or name)   # nobody writes "Iran, Islamic Republic of"
            # a declared list label ("Red Sea / Bab el-Mandeb / Suez",
            # "Middle East & North Africa") names each of its parts
            parts = [p.strip() for p in re.split(r"\s+(?:/|&)\s+", name) if p.strip()]
            if len(parts) > 1:
                label_forms.extend(parts)
        detail["geo"] = list(geo)
    elif kind == "place":
        geo = (key.upper(),) if re.fullmatch(r"[A-Za-z]{2}", key) else ()
    elif kind == "entity":
        profile = (entity_profiles or {}).get(key)
        if profile is not None:
            return _entity_profile_names(key, profile, gazetteer, seats)
        label_forms.append(key)
        iso = _country_code_of_name(key, gazetteer)
        geo = (iso.upper(),) if iso else ()
    country = geo[0].lower() if len(geo) == 1 else None
    homonyms = (gazetteer.get("subject_homonyms") or {}).get(country or "") or {}
    out = _Collector(homonyms.get("exclude") or ())
    for f in label_forms:
        out.add(f, "label" if kind != "entity" else "entity", "profile:label" if kind != "entity" else "entity:name")
    if kind == "place" and not geo:
        out.add(key, "label", "caller:key")
    if country:
        country_names(country, gazetteer=gazetteer, metonyms=seats, collector=out)
    for raw in forms.get(key, ()) if kind == "entity" else ():
        cleaned = clean_surface_form(raw)
        if cleaned:
            out.add(cleaned, "entity", "entity:merged_aliases")
    return SubjectNames(kind, key, tuple(out.names), tuple(homonyms.get("exclude") or ()), detail)


def _entity_profile_names(
    key: str, profile: Mapping[str, Any], gazetteer: Mapping[str, Any], seats: frozenset[str],
) -> SubjectNames:
    """Names for an entity subject keyed by its profile id."""
    canonical = str(profile.get("canonical_name") or "")
    raw_aliases = profile.get("aliases") or ()
    if isinstance(raw_aliases, str):
        raw_aliases = (raw_aliases,)
    iso = _country_code_of_name(canonical, gazetteer) if canonical else None
    homonyms = (gazetteer.get("subject_homonyms") or {}).get(iso or "") or {}
    out = _Collector(homonyms.get("exclude") or ())
    for i, raw in enumerate([canonical, *raw_aliases]):
        source = "entity:canonical_name" if i == 0 else "entity:merged_aliases"
        for form, kind in entity_name_forms(str(raw or "")):
            proper = is_proper_form(form)
            out.add(form, kind if proper else "entity_generic", source, usable=proper)
    if iso:
        country_names(iso, gazetteer=gazetteer, metonyms=seats, collector=out)
    detail = {"entity_class": profile.get("entity_class"), "canonical_name": canonical}
    return SubjectNames("entity", key, tuple(out.names), tuple(homonyms.get("exclude") or ()), detail)


def _is_iso2(code: str) -> bool:
    try:
        import pycountry

        return len(code) == 2 and pycountry.countries.get(alpha_2=code.upper()) is not None
    except Exception:  # noqa: BLE001 - no pycountry: no code is confirmed
        return False


def _country_code_of_name(name: str, gazetteer: Mapping[str, Any]) -> str | None:
    folded = normalize_for_match(name)
    for section in ("target_slug_names", "slug_to_country", "desk_countries"):
        for code, names in (gazetteer.get(section) or {}).items():
            if len(code) == 2 and folded in {normalize_for_match(n) for n in names}:
                return code
    return None


def related_subjects(
    subject_key: str,
    *,
    subjects: Mapping[str, Any] | None = None,
    gazetteer: Mapping[str, Any] | None = None,
) -> tuple[tuple[str, str], ...]:
    """The countries a TARGET spans, as ``(subject_kind, subject_key)``: its
    declared geo codes (more than one), and the declared countries its label
    names (``US–Iran War`` → the United States, Iran). A code that is itself a
    declared country desk resolves to that desk (``target``), else to a
    ``place`` keyed by the code. Code-chosen; never a model."""
    subjects = _default_subjects() if subjects is None else subjects
    gazetteer = _default_gazetteer() if gazetteer is None else gazetteer
    decl = subjects.get(subject_key) or {}
    codes = [str(g).upper() for g in (decl.get("geo") or ())]
    if len(codes) == 1:
        return ()   # a single-country desk: its subject IS that country
    label = str(decl.get("name") or "")
    tokens = gazetteer.get("target_slug_names") or {}
    for code, names in tokens.items():
        if not _is_iso2(code):
            continue
        for n in list(names) + list((gazetteer.get("slug_to_abbrev") or {}).get(code) or ()):
            exact = not any(ch.islower() for ch in n)
            if name_in_text(SubjectName(n, "short", "x"), label) and (exact or len(n) > 3):
                codes.append(code.upper())
                break
    desk_of = {tuple(d.get("geo") or ()): k for k, d in subjects.items()
               if len(d.get("geo") or ()) == 1 and str(d.get("domain") or "") == "geo"
               and k.startswith("country_")}
    out: list[tuple[str, str]] = []
    for code in dict.fromkeys(codes):
        desk = desk_of.get((code,))
        item = ("target", desk) if desk else ("place", code)
        if item[1] != subject_key and item not in out:
            out.append(item)
    return tuple(out)


__all__ = [
    "USABLE_KINDS",
    "SubjectName",
    "SubjectNames",
    "clean_surface_form",
    "country_names",
    "default_metonyms",
    "entity_name_forms",
    "is_proper_form",
    "label_name",
    "name_in_text",
    "related_subjects",
    "resolve_subject_names",
    "uncomma",
]
