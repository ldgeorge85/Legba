# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The slice side of the tag spine (W8 TAX-1) — one home for the predicates a
SCOPED signal slice carries beyond its window, so the two slice readers
(``runtime.actor_substrate_slice._read_substrate_slice`` and
``data.analysts.cross_target_raw.read_cross_target_slice``) cannot spell them
two ways.

Three things live here:

* ``CANONICAL_ONLY_SQL`` — the canonical-only clause, named once. A re-polled
  alias of a signal (dedup stamps its ``canonical_signal_id`` to the kept row)
  never enters a slice as a second, independent citation.

* The SIGNAL-TAG filter. Declared in two places, applied as SQL on the slice's
  one statement (never post-query, so a LIMIT is never starved by it):

    - the TARGET's ``scope.signal_tags`` (include: ``tags && $n``) and
      ``scope.exclude_signal_tags`` (exclude: ``NOT (tags && $m)``) — what the
      target IS; the include is strict, an untagged signal is not in it;
    - the ANALYST's ``subscription.substrate.signal_tags`` with
      ``subscription.substrate.untagged_fallback`` (default true): include
      ``(tags && $n OR tags = '{}')`` — the transition-era variant, because
      ~two thirds of the substrate carries no tag at all and a strict filter
      would starve the analyst of everything not yet classified. ``false``
      makes it strict.

  Every declared tag is expanded through ``tags.signal_tag_match_forms`` — THE
  legacy equivalence rule — before it reaches SQL: ``src:x`` and a bare ``x``
  match both the stored ``src:x`` and the stored legacy ``x``; ``demo.axis:x``
  matches only itself. ``SignalTagFilter.admits`` is the same predicate in
  Python (the cross-target reader attributes rows with it).

  ``signals.tags`` is ``NOT NULL DEFAULT '{}'``, so the exclude's NOT never
  meets a NULL. The column is GIN-indexed (``signals_tags_gin``).

  Lane K-1 adds the TEXT ARM: a target's ``scope.signal_text_terms`` is OR-ed
  into its include (``(tags && $n OR <title+summary> ~* $m)``), so a desk with
  no place (the cyber desk) reads its own tagged feeds AND any general-news
  row that names one of its terms, in SQL, before the LIMIT. A target that
  declares no terms gets byte-identical clauses.

* ``slice_per_source_cap`` — the per-source diversity cap knob
  (``method.options.slice_per_source_cap``, declared in the X-1 kind catalog
  for ``inline_target`` and ``cross_target_raw``) and ``strict_per_source_cap``,
  which enforces it: at most K rows per ``source_id``, NO back-fill. Absent ⇒
  ``None`` ⇒ the reader does exactly what it did before the knob existed.

* The HOMONYM exclusion (B6, W11-F13) — ``HomonymSliceFilter``. A desk scoped
  by its subject's code (``geo && ['KP']``) also receives every row the ingest
  geocoder filed under that code off a HOMONYM ("KP" = Khyber Pakhtunkhwa in
  Pakistani copy). For each code of the target's geo that the profile
  gazetteer's ``subject_homonyms`` declares, a row whose title + summary is
  homonym-only (:mod:`legba.data.subject_homonyms`) never enters the slice.
  SQL on the same statement, after the geo clause (so the GEO-v2 recovery
  leg's snapshot is untouched); NOTHING is appended for a target none of whose
  codes has a rule, so its statement is byte-identical.

``signal_tag_refusals`` is the registration-time rule: a declared tag that
``bind_tag`` would leave ``unclassified`` (a family the registry has not
loaded, or a value the family does not have) is refused by name at PUT.

A leaf: stdlib + ``legba.data.tags`` (itself stdlib-only) + ``legba.data.subject_homonyms``
(a leaf over the gazetteer leaf), so the slim
registry image imports it. The option catalog is imported lazily, inside the
one function that needs it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .subject_homonyms import HomonymRule, rule_for_code
from .tags import (
    STATUS_UNCLASSIFIED,
    VocabularyIndex,
    bind_tag,
    signal_tag_match_forms,
)

logger = logging.getLogger(__name__)

#: The canonical-only clause — the one spelling (byte-identical to the literal
#: both slice readers carried since C2b).
CANONICAL_ONLY_SQL = "(canonical_signal_id IS NULL OR canonical_signal_id = id)"


#: Lane N-X — an incident AT SEA reaches the desks of the coasts it lies off.
#: The ingest files an authoritative point in no country under NO country
#: (``signals.geo = '{}'``, the ``at_sea`` tag, ``payload.geo.at_sea``) and
#: records ``payload.geo.inferred`` = the coastal states within 200 km. This is
#: the desk-scope test: the row is FILED under one of the desk's codes, or it
#: is an unfiled at-sea row whose inferred coasts name one of them. The
#: ``tags @> ARRAY['at_sea']`` conjunct is what keeps the second arm an
#: indexed read (``signals_tags_gin``; the planner ORs it with
#: ``signals_geo_gin``); the payload test is the authority. Storage is
#: untouched, so nothing that reads ``signals.geo`` (counts, maps, bands)
#: sees an at-sea incident as an in-country one. The SAME ``$n`` binds both
#: arms, so no parameter is added. Returned WITHOUT outer parentheses: the
#: slice wraps it as ``(...)`` and the GEO-v2 recovery leg as ``NOT (...)``.
def desk_geo_sql(n: int) -> str:
    """The desk geo test over the ``text[]`` parameter ``$n``."""
    return (
        f"geo && ${n}::text[] OR (tags @> ARRAY['at_sea']::text[]"
        f" AND geo = '{{}}'::text[]"
        f" AND (payload->'geo'->>'at_sea') IS NOT DISTINCT FROM 'true'"
        f" AND EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN"
        f" jsonb_typeof(payload->'geo'->'inferred') = 'array'"
        f" THEN payload->'geo'->'inferred' END) AS e"
        f" WHERE e->>'iso2' = ANY(${n}::text[])))"
    )


#: W11-F135 - the most characters of a signal's ``payload->>'summary'`` that
#: any query-time ``to_tsvector`` may read. Postgres refuses a tsvector over
#: 1,048,575 bytes ("string is too long for tsvector"), and a summary that is
#: an inline base64 image splits into millions of unique "words"; one such row
#: (11 MB, two armscontrol.org entries, 2026-09-29/30) failed EVERY
#: ``search_signals`` call for five weeks because the planner builds the vector
#: for every row. ``left(summary, FTS_SUMMARY_MAX_CHARS)`` keeps the vector
#: under 1 MB by construction (100k chars is at most 400 KB of UTF-8, and a
#: lexeme is never longer than the text it came from) while a real summary
#: (a few KB) is read whole. ONE constant, imported by every signals-FTS site
#: (``search_signals``, ``get_timeline``), so the two stay byte-identical and an
#: expression index can name the same text.
FTS_SUMMARY_MAX_CHARS = 100_000

#: The summary leg of the signals title+summary FTS expression, capped.
FTS_SUMMARY_SQL = (
    f"left(coalesce(payload->>'summary',''), {FTS_SUMMARY_MAX_CHARS})"
)


def canonical_only_sql_for(alias: str) -> str:
    """``CANONICAL_ONLY_SQL`` with every column qualified by ``alias`` (a
    statement that reads ``signals s``); ``""`` is the bare constant."""
    if not alias:
        return CANONICAL_ONLY_SQL
    return (
        f"({alias}.canonical_signal_id IS NULL "
        f"OR {alias}.canonical_signal_id = {alias}.id)"
    )

#: Target ``scope`` keys.
SCOPE_SIGNAL_TAGS = "signal_tags"
SCOPE_EXCLUDE_SIGNAL_TAGS = "exclude_signal_tags"
#: Analyst ``subscription.substrate`` keys (an open dict on the schema; the two
#: keys are type-checked by ``SubscriptionBlock``).
SUBSTRATE_SIGNAL_TAGS = "signal_tags"
SUBSTRATE_UNTAGGED_FALLBACK = "untagged_fallback"
DEFAULT_UNTAGGED_FALLBACK = True

#: The X-1 kind-catalog knob name and the kinds that declare it.
SLICE_PER_SOURCE_CAP_OPTION = "slice_per_source_cap"
SLICE_PER_SOURCE_CAP_KINDS: frozenset[str] = frozenset({"inline_target", "cross_target_raw"})


def match_forms(tags: Iterable[Any]) -> tuple[str, ...]:
    """The stored-string forms of ``tags`` (first-seen order, deduplicated)."""
    out: list[str] = []
    for t in tags or ():
        for f in signal_tag_match_forms(t):
            if f not in out:
                out.append(f)
    return tuple(out)


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    return [t for t in raw if isinstance(t, str) and t.strip()]


@dataclass(frozen=True)
class SignalTagFilter:
    """One include/exclude pair over ``signals.tags``, in stored-string forms.

    ``include`` empty = no include filter; ``exclude`` empty = no exclude.
    ``untagged_fallback`` admits an untagged signal past the include.
    ``text_terms`` (lane K-1, a target's ``scope.signal_text_terms``) is a
    SECOND include arm, OR-ed with the tag arm: a row is in when it carries
    one of the tags OR its title + summary names one of the terms (word-start,
    case-insensitive; :func:`text_terms_regex`). Empty = no text arm, and the
    clauses are byte-identical to the pre-K-1 ones.
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    untagged_fallback: bool = False
    text_terms: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.include or self.exclude or self.text_terms)

    def sql(self, params: list[Any]) -> list[str]:
        """The clauses, with their arrays appended to ``params`` (positional
        ``$n`` numbering continues from ``len(params)``). ``[]`` when inactive."""
        out: list[str] = []
        if self.include:
            params.append(list(self.include))
            inc = f"tags && ${len(params)}::text[]"
            if self.text_terms:
                params.append(text_terms_regex(self.text_terms, dialect="postgres"))
                inc = f"({inc} OR {SLICE_TEXT_SQL} ~* ${len(params)})"
            out.append(f"({inc} OR tags = '{{}}')" if self.untagged_fallback else inc)
        elif self.text_terms:
            params.append(text_terms_regex(self.text_terms, dialect="postgres"))
            out.append(f"{SLICE_TEXT_SQL} ~* ${len(params)}")
        if self.exclude:
            params.append(list(self.exclude))
            out.append(f"NOT (tags && ${len(params)}::text[])")
        return out

    def admits(self, tags: Iterable[Any] | None, text: str | None = None) -> bool:
        """The SQL predicate, in Python, over one row's ``tags`` (and, for a
        filter with a text arm, the row's :func:`slice_text`; absent text never
        matches the text arm)."""
        have = {t for t in (tags or ()) if isinstance(t, str)}
        if self.exclude and have & set(self.exclude):
            return False
        if self.include or self.text_terms:
            if self.include and have & set(self.include):
                return True
            if self.text_terms and text and text_terms_match(self.text_terms, text):
                return True
            return bool(self.include) and self.untagged_fallback and not have
        return True


NO_FILTER = SignalTagFilter()


# ---------------------------------------------------------------------------
# The text arm (lane K-1): a desk that has no place selects by what it is about
# ---------------------------------------------------------------------------

#: Target ``scope`` key: the terms of the text arm.
SCOPE_SIGNAL_TEXT_TERMS = "signal_text_terms"
#: At most this many terms per target, each at most this many characters.
MAX_TEXT_TERMS = 64
MAX_TEXT_TERM_LENGTH = 48
#: A term: lowercase letters and digits, words joined by ONE space or hyphen,
#: an optional trailing hyphen (``cve-`` = every CVE identifier). Lowercase
#: only, so a declaration reads exactly as it matches (case-insensitively),
#: and no character in a term is a regex metacharacter.
_TEXT_TERM_RE = re.compile(r"^[a-z0-9]+(?:[ -][a-z0-9]+)*-?$")

#: The text the arm reads: the same title + summary (or distilled body) the
#: homonym exclusion judges (:data:`HOMONYM_TEXT_SQL`, below), one spelling.
#: Resolved at call time so the constant keeps its place beside that rule.


def text_term_error(term: Any) -> str | None:
    """Why ``term`` is not a valid text-arm term, or ``None``."""
    if not isinstance(term, str):
        return "a text term must be a string"
    if len(term) > MAX_TEXT_TERM_LENGTH:
        return f"text term {term!r} is longer than {MAX_TEXT_TERM_LENGTH} characters"
    if not _TEXT_TERM_RE.match(term):
        return (
            f"text term {term!r} must be lowercase letters and digits, words "
            "joined by one space or hyphen (a trailing hyphen is allowed)"
        )
    return None


def text_terms_regex(terms: Iterable[str], *, dialect: str) -> str:
    """ONE alternation over ``terms``. Each term matches at the START of a
    word (a stem matches its inflections: ``hacker`` matches ``hackers``), and
    a space or hyphen inside a term matches a space, a hyphen, or nothing
    (``cyber attack`` matches ``cyberattack``, ``cyber-attack`` and
    ``cyber attack``). ``dialect`` is ``postgres`` (ARE, ``\\m`` = word
    start; applied with ``~*``) or ``python`` (``\\b``; searched with
    IGNORECASE). The terms are validated, so the joiners are the only
    metacharacters in the pattern."""
    if dialect not in ("postgres", "python"):
        raise ValueError(f"unknown regex dialect {dialect!r}")
    alts: list[str] = []
    for term in terms:
        err = text_term_error(term)
        if err is not None:
            raise ValueError(err)
        words = re.split(r"[ -]", term.rstrip("-"))
        body = r"[\s-]?".join(words) + ("-" if term.endswith("-") else "")
        if body not in alts:
            alts.append(body)
    if not alts:
        raise ValueError("text_terms_regex needs at least one term")
    start = r"\m" if dialect == "postgres" else r"\b"
    return start + "(?:" + "|".join(alts) + ")"


def text_terms_match(terms: Iterable[str], text: str) -> bool:
    """The text arm, in Python, over one row's :func:`slice_text`."""
    pattern = text_terms_regex(tuple(terms), dialect="python")
    return re.search(pattern, text or "", re.IGNORECASE) is not None


def slice_text(payload: Mapping[str, Any] | None) -> str:
    """:data:`SLICE_TEXT_SQL`, in Python: the title, a space, then the summary
    (or, when the payload has no ``summary`` value, the distilled body)."""
    p = payload if isinstance(payload, Mapping) else {}

    def _s(key: str) -> str | None:
        v = p.get(key)
        return None if v is None else str(v)

    summary = _s("summary")
    rest = summary if summary is not None else (_s("distilled_body") or "")
    return (_s("title") or "") + " " + rest


def target_tag_filter(scope: Mapping[str, Any] | None) -> SignalTagFilter:
    """The filter a target body's ``scope`` declares (strict include, and the
    text arm when ``scope.signal_text_terms`` is declared)."""
    if not isinstance(scope, Mapping):
        return NO_FILTER
    terms = tuple(t for t in _str_list(scope.get(SCOPE_SIGNAL_TEXT_TERMS))
                  if text_term_error(t) is None)
    return SignalTagFilter(
        include=match_forms(_str_list(scope.get(SCOPE_SIGNAL_TAGS))),
        exclude=match_forms(_str_list(scope.get(SCOPE_EXCLUDE_SIGNAL_TAGS))),
        text_terms=terms,
    )


def analyst_tag_filter(descriptor: Any) -> SignalTagFilter:
    """The filter an analyst's ``subscription.substrate`` declares."""
    sub = getattr(descriptor, "subscription", None)
    substrate = getattr(sub, "substrate", None) if sub is not None else None
    if not isinstance(substrate, Mapping):
        return NO_FILTER
    include = match_forms(_str_list(substrate.get(SUBSTRATE_SIGNAL_TAGS)))
    if not include:
        return NO_FILTER
    fallback = substrate.get(SUBSTRATE_UNTAGGED_FALLBACK, DEFAULT_UNTAGGED_FALLBACK)
    return SignalTagFilter(
        include=include,
        untagged_fallback=fallback if isinstance(fallback, bool) else DEFAULT_UNTAGGED_FALLBACK,
    )


# ---------------------------------------------------------------------------
# The homonym exclusion (B6, W11-F13)
# ---------------------------------------------------------------------------

#: The text a slice row is judged on — the title and the summary (or the
#: distilled body), the prose the geocoder's lead zone and a reader see.
HOMONYM_TEXT_SQL = (
    "(coalesce(payload->>'title','') || ' ' || "
    "coalesce(payload->>'summary', payload->>'distilled_body', ''))"
)
#: Lane K-1 — the text the slice's text arm reads: the SAME expression.
SLICE_TEXT_SQL = HOMONYM_TEXT_SQL


def _param(params: list[Any], value: Any) -> str:
    params.append(value)
    return f"${len(params)}"


def _matches_sql(expr: str, cs: str | None, ci: str | None) -> str:
    """``expr`` matches the case-sensitive OR the case-insensitive pattern
    (each a ``$n`` already bound, or ``None``)."""
    arms = [f"{expr} ~ {cs}" if cs else "", f"{expr} ~* {ci}" if ci else ""]
    return "(" + " OR ".join(a for a in arms if a) + ")" if any(arms) else "FALSE"


@dataclass(frozen=True)
class HomonymSliceFilter:
    """The homonym exclusion over one target's geo codes.

    ``plain`` = the target's codes with no rule (a row on any of them is
    admitted as before); ``rules`` = the codes with one. A row is admitted
    when SOME code it shares with the target admits it: a plain code, or a
    ruled code whose text is not homonym-only."""

    plain: tuple[str, ...] = ()
    rules: tuple[HomonymRule, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.rules)

    def sql(self, params: list[Any], *, admit_unfiled: bool = False) -> list[str]:
        """The one clause (``[]`` when inactive), params appended in order.

        ``admit_unfiled`` (the desk slice, lane N-X) adds a leading
        ``geo = '{}'`` arm: the homonym rule judges a FILING, and an unfiled
        row reaches the slice only through the at-sea arm of
        :func:`desk_geo_sql`, so it has no filing to judge."""
        if not self.rules:
            return []
        arms: list[str] = ["geo = '{}'::text[]"] if admit_unfiled else []
        if self.plain:
            params.append(list(self.plain))
            arms.append(f"geo && ${len(params)}::text[]")
        text = HOMONYM_TEXT_SQL
        for rule in self.rules:
            on_code = f"geo && {_param(params, [rule.geo_code])}::text[]"
            hom_cs, hom_ci, subj_cs, subj_ci = (
                None if p is None else _param(params, p) for p in rule.pg_patterns()
            )
            cut = text
            for bound, flags in ((hom_cs, "g"), (hom_ci, "gi")):
                if bound:
                    cut = f"regexp_replace({cut}, {bound}, ' ', '{flags}')"
            homonym_only = (
                f"{_matches_sql(text, hom_cs, hom_ci)} AND NOT "
                f"{_matches_sql(cut, subj_cs, subj_ci)}"
            )
            arms.append(f"({on_code} AND NOT ({homonym_only}))")
        return ["(" + " OR ".join(arms) + ")"]

    def admits(self, geo: Iterable[Any] | None, text: str | None) -> bool:
        """The SQL predicate, in Python, over one row's ``geo`` and text."""
        have = {g for g in (geo or ()) if isinstance(g, str)}
        if have & set(self.plain):
            return True
        return any(
            r.geo_code in have and not r.homonym_only(text or "") for r in self.rules
        )


NO_HOMONYM_FILTER = HomonymSliceFilter()


def homonym_slice_filter(target_geo: Iterable[Any] | None) -> HomonymSliceFilter:
    """The homonym exclusion for a target's ``scope.geo`` codes."""
    codes = [g for g in (target_geo or ()) if isinstance(g, str) and g]
    ruled = [r for r in (rule_for_code(c) for c in codes) if r is not None]
    if not ruled:
        return NO_HOMONYM_FILTER
    ruled_codes = {r.geo_code for r in ruled}
    return HomonymSliceFilter(
        plain=tuple(c for c in codes if c.upper() not in ruled_codes),
        rules=tuple(ruled),
    )


# ---------------------------------------------------------------------------
# Registration-time validation
# ---------------------------------------------------------------------------


def declared_signal_tags(descriptor: Any) -> list[str]:
    """Every signal tag a target or analyst descriptor declares (target:
    ``scope.signal_tags`` + ``scope.exclude_signal_tags``; analyst:
    ``subscription.substrate.signal_tags``)."""
    scope = getattr(descriptor, "scope", None)
    if scope is not None:
        return list(getattr(scope, SCOPE_SIGNAL_TAGS, None) or []) + list(
            getattr(scope, SCOPE_EXCLUDE_SIGNAL_TAGS, None) or []
        )
    sub = getattr(descriptor, "subscription", None)
    substrate = getattr(sub, "substrate", None) if sub is not None else None
    if isinstance(substrate, Mapping):
        return _str_list(substrate.get(SUBSTRATE_SIGNAL_TAGS))
    return []


def signal_tag_refusals(
    tags: Iterable[Any], index: VocabularyIndex,
) -> tuple[list[str], dict[str, list[str]]]:
    """``(unknown_families, {family: unknown_values})`` for the declared tags
    ``bind_tag`` would leave ``unclassified`` against ``index``. A bare tag
    (``legacy_src``) and a bound one are fine. Both empty = admissible."""
    families: list[str] = []
    values: dict[str, list[str]] = {}
    for t in tags or ():
        b = bind_tag(t, families=index)
        if b.status != STATUS_UNCLASSIFIED:
            continue
        if not index.family_loaded(b.family):
            if b.family not in families:
                families.append(b.family or repr(t))
        else:
            values.setdefault(b.family, []).append(b.value)
    return families, values


# ---------------------------------------------------------------------------
# The per-source diversity cap
# ---------------------------------------------------------------------------


def slice_per_source_cap(descriptor: Any) -> int | None:
    """The descriptor's declared ``method.options.slice_per_source_cap`` for
    a kind that declares the knob, validated through the kind catalog's
    ``OptionSpec`` — ``None`` when absent, undeclared for the kind, or
    invalid (an invalid value logs a WARNING and the reader keeps today's
    behaviour, the X-1 loud-degrade contract)."""
    identity = getattr(descriptor, "identity", None)
    kind_raw = getattr(identity, "kind", None)
    kind = str(getattr(kind_raw, "value", kind_raw) or "")
    if kind not in SLICE_PER_SOURCE_CAP_KINDS:
        return None
    options = getattr(getattr(descriptor, "method", None), "options", None)
    if not isinstance(options, Mapping) or SLICE_PER_SOURCE_CAP_OPTION not in options:
        return None
    from .option_catalog.handler_options import ANALYST_KIND_OPTIONS

    spec = next(
        (s for s in ANALYST_KIND_OPTIONS.get(kind, ())
         if s.name == SLICE_PER_SOURCE_CAP_OPTION),
        None,
    )
    if spec is None:                                        # pragma: no cover
        return None
    ok, value, cause = spec.validate(options[SLICE_PER_SOURCE_CAP_OPTION])
    if not ok:
        logger.warning(
            "signal_slice.per_source_cap.invalid analyst=%s value=%r cause=%s — "
            "the slice keeps its default diversity behaviour",
            getattr(identity, "id", "?"), options[SLICE_PER_SOURCE_CAP_OPTION], cause,
        )
        return None
    return int(value)


def strict_per_source_cap(rows: list[Any], *, per_source_cap: int, limit: int) -> list[Any]:
    """At most ``per_source_cap`` rows per ``source_id``, in input (recency)
    order, up to ``limit``. No back-fill: a slice short of ``limit`` stays
    short rather than letting one source past its cap."""
    kept: list[Any] = []
    seen: dict[Any, int] = {}
    for r in rows:
        if len(kept) >= limit:
            break
        sid = r["source_id"]
        if seen.get(sid, 0) < per_source_cap:
            kept.append(r)
            seen[sid] = seen.get(sid, 0) + 1
    return kept


__all__ = [
    "CANONICAL_ONLY_SQL",
    "DEFAULT_UNTAGGED_FALLBACK",
    "FTS_SUMMARY_MAX_CHARS",
    "FTS_SUMMARY_SQL",
    "HOMONYM_TEXT_SQL",
    "NO_FILTER",
    "NO_HOMONYM_FILTER",
    "SCOPE_EXCLUDE_SIGNAL_TAGS",
    "SCOPE_SIGNAL_TAGS",
    "SCOPE_SIGNAL_TEXT_TERMS",
    "SLICE_TEXT_SQL",
    "SLICE_PER_SOURCE_CAP_KINDS",
    "SLICE_PER_SOURCE_CAP_OPTION",
    "SUBSTRATE_SIGNAL_TAGS",
    "SUBSTRATE_UNTAGGED_FALLBACK",
    "HomonymSliceFilter",
    "SignalTagFilter",
    "analyst_tag_filter",
    "canonical_only_sql_for",
    "declared_signal_tags",
    "desk_geo_sql",
    "homonym_slice_filter",
    "match_forms",
    "signal_tag_refusals",
    "slice_per_source_cap",
    "strict_per_source_cap",
    "slice_text",
    "target_tag_filter",
    "text_term_error",
    "text_terms_match",
    "text_terms_regex",
]
