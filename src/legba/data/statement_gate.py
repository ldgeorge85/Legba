# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The statement layer's CODE gates — the rules that keep memory honest,
as pure functions (plan P1 §3; lane S2).

Each gate is a rule code enforces and the failure it stops:

========  ==================================================  =========================================
Gate      Rule                                                Failure it stops
========  ==================================================  =========================================
G1 quote  every evidence/contradicts quote occurs verbatim     the fabricated claim (no signal behind it)
          (under the house fold) in the stored text of the
          cited fetch version — or the signal's stored text
          when no receipt exists — and the row records which;
          a quote found only in the signal's FEED ITEM (its
          stored title / summary) passes recorded as such
          (``quote_found_in``) and is not checkable (S-5)
G2        numbers stated appear in a quote or the cited        numbers drifting between source and
content   observation (date parts are never quantities);       record; a party's claim recorded as fact
          dates by the rule in ``statement_dates``; one of
          the subject's names (``statement_subject_names``)
          is in a quote or title (a typed counterpart's too);
          ``valid_from`` is stated and ≤ publication; a
          claim-class source supports only ``attributed``
G3        a run may cite only rows it was handed               citations invented or remembered
G7        corroboration counts origin groups after             corroboration by repetition
          near-duplicate collapse (counts; never refuses)
G8        a statement is never evidence; a fact is background  recycled conclusions as corroboration
          only; an assessment never supports a factual
          statement; establishment needs an evidence row
G11 text  no model/harness control text in a statement or a    injected instructions carried as memory
          quote (the journal gate's one pattern list)
G13       the ceiling is the most restrictive access class of  private material becoming shared record
          the support, computed at write
========  ==================================================  =========================================

WHAT IS NOT HERE. G4 (the clean-context support check) is a model seat (lane
S3); G5/G6/G9/G10/G12 are lifecycle, store and wiring rules (lanes S1, S6–S8).
:func:`evaluate_code_gates` answers "did the code checks pass" — never
"established".

PURE. No I/O, no model, no table. Inputs are the protocols in
:mod:`legba.data.statement_shapes`; the store's row type, whichever table it
lands in, satisfies them by attribute.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any

from .journal_evidence import find_control_text
from .provenance.access import ACCESS_CLASSES
from .provenance.text_fold import FOLD_VERSION, fold_contains, normalize_for_match
from .statement_content import evidence_number_keys, strip_markers
from .statement_dates import EvidenceDating, judge_dates, valid_from_stated
from .statement_numbers import evidence_figures, unsupported_figures
from .statement_independence import (
    contradicting_groups,
    count_groups,
    derive_support_word,
)
from .statement_subject_names import SubjectName, name_in_text
from .statement_shapes import (
    SLOT_NAMES,
    UNDECLARED_ACCESS,
    CheckResult,
    EvidenceTexts,
    GateReport,
    HandedSlice,
    QuoteFoundIn,
    SourceClass,
    StatementKind,
    StatementLike,
    SubjectKind,
    SupportKind,
    SupportLike,
    SupportRole,
    TextOrigin,
    ValidFromPrecision,
    coerce_source_class,
    enum_value,
    text_sha256,
    values_of,
)

#: A quote shorter than this many words checks nothing ("the" occurs in every
#: article). Tunable; a refusal leaves the statement a candidate, never lost.
MIN_QUOTE_WORDS = 3
#: Plan §2.6 — "At most five premises per statement."
MAX_PREMISES = 5

#: Roles whose quote G1 checks. ``contradicts`` is included: a fabricated
#: contradiction would contest a good statement (plan §2.6).
QUOTED_ROLES = frozenset({SupportRole.EVIDENCE.value, SupportRole.CONTRADICTS.value})
#: Support kinds that are rows a run is handed (G3).
SLICE_KINDS = frozenset({SupportKind.SIGNAL.value, SupportKind.OBSERVATION.value})

#: Access classes in restriction order, for the ceiling. ``undeclared`` sits
#: just above ``public``: it is shown as public but never promoted to it, so a
#: statement with any undeclared support carries ``undeclared``, never
#: ``public``; every declared restriction outranks it.
ACCESS_RANK: tuple[str, ...] = (
    ACCESS_CLASSES[0], UNDECLARED_ACCESS, *ACCESS_CLASSES[1:],
)

_KINDS = values_of(list(StatementKind))
_SUBJECT_KINDS = values_of(list(SubjectKind))
_ROLES = values_of(list(SupportRole))
_SUPPORT_KINDS = values_of(list(SupportKind))
_PRECISIONS = values_of(list(ValidFromPrecision))


def _role(row: SupportLike) -> str:
    return enum_value(row.role)


def _skind(row: SupportLike) -> str:
    return enum_value(row.support_kind)


def _evidence(support: Sequence[SupportLike]) -> list[tuple[int, SupportLike]]:
    return [(i, r) for i, r in enumerate(support) if _role(r) == SupportRole.EVIDENCE.value]


# ---------------------------------------------------------------------------
# G1 — the quote is in the text the row cites
# ---------------------------------------------------------------------------


def resolve_text(row: SupportLike, texts: EvidenceTexts) -> tuple[str | None, TextOrigin | None]:
    """The stored text a support row's quote must be found in, and which.

    A signal with a receipt resolves ONLY to that receipt's version: a quote
    present in another version of the page is not support for this row."""
    kind = _skind(row)
    ref = str(row.ref_id)
    if kind == SupportKind.SIGNAL.value:
        if row.fetch_version_id is not None:
            item = texts.by_fetch_item.get((row.fetch_version_id, ref))
            if item is not None:
                return item, TextOrigin.FETCH_ITEM
            return texts.by_fetch_version.get(row.fetch_version_id), TextOrigin.FETCH_VERSION
        return texts.by_signal.get(ref), TextOrigin.SIGNAL_TEXT
    if kind == SupportKind.OBSERVATION.value:
        obs = texts.by_observation.get(ref)
        return (obs.text if obs is not None else None), TextOrigin.OBSERVATION
    if kind == SupportKind.KNOWLEDGE_ITEM.value:
        return texts.by_knowledge_item_version.get(ref), TextOrigin.KNOWLEDGE_ITEM_VERSION
    return None, None


def feed_item_texts(row: SupportLike, texts: EvidenceTexts) -> tuple[str, ...]:
    """The signal's FEED ITEM texts (stored title, summary) the caller
    resolved for ``row``; empty for any other kind or when none were given."""
    if _skind(row) != SupportKind.SIGNAL.value:
        return ()
    parts = (getattr(texts, "feed_item_by_signal", None) or {}).get(str(row.ref_id))
    if parts is None:
        return ()
    if isinstance(parts, str):
        parts = (parts,)
    return tuple(p for p in parts if isinstance(p, str) and p.strip())


def locate_quote(row: SupportLike, texts: EvidenceTexts) -> str | None:
    """WHICH text of a signal row the quote is in (:class:`QuoteFoundIn`
    value): the page text (the receipt's text, or the stored body without
    one), the feed item (title and summary each on its own — a quote may not
    straddle them), both, or ``None`` (neither, or not a signal row)."""
    if _skind(row) != SupportKind.SIGNAL.value:
        return None
    quote = row.quote or ""
    if not normalize_for_match(quote):
        return None
    page, _ = resolve_text(row, texts)
    in_page = bool(page) and fold_contains(page, quote)
    in_feed = any(fold_contains(t, quote) for t in feed_item_texts(row, texts))
    if in_page and in_feed:
        return QuoteFoundIn.BOTH.value
    if in_page:
        return QuoteFoundIn.PAGE_TEXT.value
    if in_feed:
        return QuoteFoundIn.FEED_ITEM.value
    return None


def check_quote(row: SupportLike, texts: EvidenceTexts, *, row_index: int | None = None) -> CheckResult:
    """G1 for one support row.

    Fails with ``quote_missing``, ``quote_too_short``, ``text_unavailable``,
    ``text_hash_mismatch`` or ``quote_absent``; passes with the text origin,
    the text's sha256 (for the store's ``text_sha256``) and, for a signal read
    without a receipt, ``no_receipt: true``. Rows outside :data:`QUOTED_ROLES`
    and kinds with no stored text (statement, fact) pass as not applicable.

    S-5: a signal row whose feed item the caller resolved also records
    ``quote_found_in`` (``page_text`` / ``feed_item`` / ``both``). A quote
    found ONLY in the feed item passes with ``checkability: feed_item_only``:
    the page the receipt archived does not carry it — a finding about the
    evidence, kept on the row, never hidden."""
    role, kind = _role(row), _skind(row)
    if role not in QUOTED_ROLES or kind in {SupportKind.STATEMENT.value, SupportKind.FACT.value}:
        return CheckResult("G1", "quote", True, None, row_index, {"applies": False})
    detail: dict[str, Any] = {"fold_version": FOLD_VERSION}
    quote = row.quote or ""
    if not normalize_for_match(quote):
        return CheckResult("G1", "quote", False, "quote_missing", row_index, detail)
    if len(normalize_for_match(quote).split()) < MIN_QUOTE_WORDS:
        detail["min_words"] = MIN_QUOTE_WORDS
        return CheckResult("G1", "quote", False, "quote_too_short", row_index, detail)
    text, origin = resolve_text(row, texts)
    if origin is not None:
        detail["text_origin"] = origin.value
    if origin in (TextOrigin.FETCH_VERSION, TextOrigin.FETCH_ITEM):
        detail["fetch_version_id"] = row.fetch_version_id
    if origin is TextOrigin.SIGNAL_TEXT:
        detail["no_receipt"] = True
    feed = feed_item_texts(row, texts)
    in_feed = any(fold_contains(t, quote) for t in feed)
    if text is None:
        if in_feed:
            detail.update(quote_found_in=QuoteFoundIn.FEED_ITEM.value, checkability="feed_item_only",
                          page_text="unavailable")
            return CheckResult("G1", "quote", True, None, row_index, detail)
        return CheckResult("G1", "quote", False, "text_unavailable", row_index, detail)
    digest = text_sha256(text)
    detail["text_sha256"] = digest
    if row.text_sha256 and row.text_sha256.lower() != digest:
        detail["row_text_sha256"] = row.text_sha256
        return CheckResult("G1", "quote", False, "text_hash_mismatch", row_index, detail)
    in_page = fold_contains(text, quote)
    if feed:
        detail["quote_found_in"] = (QuoteFoundIn.BOTH.value if in_page and in_feed
                                    else QuoteFoundIn.PAGE_TEXT.value if in_page
                                    else QuoteFoundIn.FEED_ITEM.value if in_feed else None)
    if not in_page:
        if in_feed:
            detail["checkability"] = "feed_item_only"
            return CheckResult("G1", "quote", True, None, row_index, detail)
        return CheckResult("G1", "quote", False, "quote_absent", row_index, detail)
    return CheckResult("G1", "quote", True, None, row_index, detail)


# ---------------------------------------------------------------------------
# G2 — what the statement states, the evidence states
# ---------------------------------------------------------------------------


def _as_utc_date(value: datetime | date) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.date()
    return value


def _valid_from_floor(valid_from: datetime, precision: str) -> date:
    d = _as_utc_date(valid_from)
    return d.replace(day=1) if precision == ValidFromPrecision.MONTH.value else d


def check_numbers(
    statement: StatementLike, support: Sequence[SupportLike], texts: EvidenceTexts,
) -> CheckResult:
    """G2 numbers: every figure the statement text states is supported by a
    figure some evidence quote or cited observation states, under the S-6
    number rule (:mod:`legba.data.statement_numbers`: value, form, unit,
    qualifier, range); a numeric ``value`` slot is a value the evidence
    states. Dates are read by :func:`check_dates` and not counted twice
    here."""
    body = strip_markers(statement.text)
    quotes = [r.quote or "" for _, r in _evidence(support)]
    obs_values: list[float] = []
    for _, r in _evidence(support):
        if _skind(r) == SupportKind.OBSERVATION.value:
            obs = texts.by_observation.get(str(r.ref_id))
            if obs is not None:
                obs_values.extend(obs.values)
                quotes.append(obs.text)
    stated, unsupported = unsupported_figures(body, evidence_figures(quotes, obs_values))
    missing = [f.raw for f in unsupported]
    keys = evidence_number_keys(quotes, obs_values)
    slot_value = (statement.slots or {}).get("value")
    if isinstance(slot_value, (int, float)) and not isinstance(slot_value, bool):
        if round(float(slot_value), 6) not in keys:
            missing.append(f"slots.value={slot_value}")
    detail = {"stated": [f.raw for f in stated], "read_as": [f.label() for f in stated],
              "missing": missing}
    if missing:
        return CheckResult("G2", "numbers", False, "number_not_in_evidence", None, detail)
    return CheckResult("G2", "numbers", True, None, None, detail)


def check_dates(
    statement: StatementLike,
    support: Sequence[SupportLike],
    *,
    allow_publication_date: bool = True,
    read_at: datetime | date | None = None,
) -> CheckResult:
    """G2 dates — the rule in :mod:`legba.data.statement_dates`: a stated date
    is accepted only as (a) an evidence row's publication day or a relative
    word in its quote resolved against it, (b) the read's own day in an as-of
    frame (stripped; returned as ``known_at``), or (c) a date a quote states.
    ``allow_publication_date=False`` withdraws rule (a)'s publication-day half
    (the S-3 configuration)."""
    body = strip_markers(statement.text)
    evidence = [EvidenceDating(r.quote or "", r.published_at) for _, r in _evidence(support)]
    verdict = judge_dates(body, evidence, read_at=read_at,
                          allow_publication_date=allow_publication_date)
    detail: dict[str, Any] = {"stated": list(verdict.stated), "missing": list(verdict.missing),
                              "accepted": {k: list(v) for k, v in verdict.accepted.items()}}
    if verdict.accepted.get("publication_day"):
        detail["matched_by_publication_date"] = list(verdict.accepted["publication_day"])
    if verdict.known_at is not None:
        detail["known_at"] = verdict.known_at.isoformat()
    if verdict.unresolved_relative:
        detail["unresolved_relative"] = list(verdict.unresolved_relative)
        return CheckResult("G2", "dates", False, "relative_date_unresolved", None, detail)
    if verdict.missing:
        return CheckResult("G2", "dates", False, "date_not_in_evidence", None, detail)
    return CheckResult("G2", "dates", True, None, None, detail)


def check_valid_from_stated(
    statement: StatementLike,
    support: Sequence[SupportLike],
    *,
    read_at: datetime | date | None = None,
) -> CheckResult:
    """G2: a typed ``valid_from`` (day or month precision) is a date the
    sentence or its evidence states — never one inferred from context.
    Unknown passes (nothing is claimed)."""
    vf = statement.valid_from
    precision = enum_value(statement.valid_from_precision) or ValidFromPrecision.DAY.value
    if vf is None or precision == ValidFromPrecision.UNKNOWN.value:
        return CheckResult("G2", "valid_from_stated", True, None, None, {"applies": False})
    evidence = [EvidenceDating(r.quote or "", r.published_at) for _, r in _evidence(support)]
    why = valid_from_stated(vf, precision, strip_markers(statement.text), evidence, read_at=read_at)
    detail = {"valid_from": _as_utc_date(vf).isoformat(), "precision": precision}
    if why is None:
        return CheckResult("G2", "valid_from_stated", False, "valid_from_not_stated", None, detail)
    return CheckResult("G2", "valid_from_stated", True, None, None, {**detail, "by": why})


def check_subject_named(
    support: Sequence[SupportLike],
    subject_names: Iterable[str | SubjectName],
    *,
    excluded: Iterable[str] | None = None,
) -> CheckResult:
    """G2 subject: one of the subject's names is in an evidence quote or an
    evidence row's title. ``subject_names`` is the caller's names for
    ``subject_key`` — plain strings, or the resolver's
    :class:`~legba.data.statement_subject_names.SubjectNames` (its usable
    names, each with kind and provenance, and the declared homonym forms a
    match may not sit inside). An all-capitals name matches case-sensitively
    (``US`` is not ``us``). An empty list cannot be checked and fails."""
    if excluded is None:
        excluded = getattr(subject_names, "excluded", ())
    excluded = tuple(excluded)
    names = [n if isinstance(n, SubjectName) else SubjectName(str(n), "given", "caller")
             for n in subject_names if n and str(getattr(n, "name", n)).strip()]
    names = [n for n in names if n.usable]
    if not names:
        return CheckResult("G2", "subject_named", False, "subject_names_missing")
    for i, r in _evidence(support):
        for where, text in (("quote", r.quote), ("title", r.title)):
            for name in names:
                if text and name_in_text(name, text, excluded=excluded):
                    return CheckResult("G2", "subject_named", True, None, None,
                                       {"name": name.name, "kind": name.kind,
                                        "source": name.source, "row": i, "in": where})
    return CheckResult("G2", "subject_named", False, "subject_not_named",
                       None, {"names": [n.name for n in names]})


def check_counterpart_named(
    support: Sequence[SupportLike],
    counterpart_names: Iterable[str | SubjectName],
) -> CheckResult:
    """G2 counterpart (S-5): a statement about a RELATION between two actors
    (subject + ``counterpart`` slot) names the counterpart in an evidence quote
    or title exactly as the subject must be — the same names rule
    (:func:`check_subject_named`), reported under its own check name."""
    res = check_subject_named(support, counterpart_names)
    reason = None if res.passed else {"subject_not_named": "counterpart_not_named",
                                      "subject_names_missing": "counterpart_names_missing"}.get(
                                          res.reason or "", res.reason)
    return CheckResult("G2", "counterpart_named", res.passed, reason, None, dict(res.detail))


def check_valid_from(statement: StatementLike, row: SupportLike, row_index: int) -> CheckResult:
    """G2 valid time, per evidence row: ``valid_from`` (at its precision) is
    not later than the row's publication day. Unknown valid-from or precision
    ``unknown`` passes (nothing is claimed); an unknown publication time fails,
    since the claim cannot be checked against it."""
    vf = statement.valid_from
    precision = enum_value(statement.valid_from_precision) or ValidFromPrecision.DAY.value
    if vf is None or precision == ValidFromPrecision.UNKNOWN.value:
        return CheckResult("G2", "valid_from", True, None, row_index, {"applies": False})
    if precision not in _PRECISIONS:
        return CheckResult("G2", "valid_from", False, "unknown_precision", row_index,
                           {"precision": precision})
    if row.published_at is None:
        return CheckResult("G2", "valid_from", False, "published_at_unknown", row_index)
    floor = _valid_from_floor(vf, precision)
    pub = _as_utc_date(row.published_at)
    detail = {"valid_from": floor.isoformat(), "precision": precision, "published": pub.isoformat()}
    if floor > pub:
        return CheckResult("G2", "valid_from", False, "valid_from_after_publication",
                           row_index, detail)
    return CheckResult("G2", "valid_from", True, None, row_index, detail)


def check_claim_source(
    statement: StatementLike,
    row: SupportLike,
    row_index: int,
    *,
    claim_values: Iterable[str] = ("claim",),
    roles: Iterable[str] = (SupportRole.EVIDENCE.value,),
) -> CheckResult:
    """G2's claim rule, per row: a row from a claim-class source, in one of
    ``roles``, supports only an ``attributed`` statement ("X claimed Y").

    ``claim_values`` maps the claim-source-class lane's raw value(s) — the
    seam that keeps this gate off that lane's field name."""
    sc = coerce_source_class(row.source_class, claim_values=claim_values)
    detail = {"source_class": sc.value}
    if _role(row) not in set(roles) or sc is not SourceClass.CLAIM:
        return CheckResult("G2", "claim_source", True, None, row_index, detail)
    if enum_value(statement.kind) == StatementKind.ATTRIBUTED.value:
        return CheckResult("G2", "claim_source", True, None, row_index, detail)
    detail["statement_kind"] = enum_value(statement.kind)
    return CheckResult("G2", "claim_source", False, "claim_source_supports_only_attributed",
                       row_index, detail)


def check_slots(statement: StatementLike) -> CheckResult:
    """G2 slots (plan §2.1 "slots kind-checked in code"): only the four closed
    slot names; a ``level`` carries a ``value``."""
    slots = dict(statement.slots or {})
    unknown = sorted(set(slots) - SLOT_NAMES)
    if unknown:
        return CheckResult("G2", "slots", False, "unknown_slot", None, {"unknown": unknown})
    if enum_value(statement.kind) == StatementKind.LEVEL.value and slots.get("value") in (None, ""):
        return CheckResult("G2", "slots", False, "level_without_value")
    return CheckResult("G2", "slots", True)


# ---------------------------------------------------------------------------
# G3 — handed rows only
# ---------------------------------------------------------------------------


def check_handed_row(row: SupportLike, handed: HandedSlice, row_index: int) -> CheckResult:
    """G3 per support row: an evidence/contradicts row of a slice kind
    (signal, observation) names a row the run was handed."""
    if _role(row) not in QUOTED_ROLES or _skind(row) not in SLICE_KINDS:
        return CheckResult("G3", "handed", True, None, row_index, {"applies": False})
    detail = {"run_id": handed.run_id}
    if row.ref_id in handed:
        return CheckResult("G3", "handed", True, None, row_index, detail)
    return CheckResult("G3", "handed", False, "not_in_run_slice", row_index,
                       {**detail, "ref_id": str(row.ref_id)})


def check_cited_ids(cited: Iterable[Any], handed: HandedSlice) -> CheckResult:
    """G3 over a desk action's cited ids (a confirmation's row, a proposal's
    resolved markers): every id was handed to this run. Refusals are counted
    in the detail (plan: "refused and counted")."""
    ids = [str(c).lower() for c in cited if c is not None]
    outside = sorted({i for i in ids if i not in handed})
    detail = {"run_id": handed.run_id, "cited": len(ids), "not_handed": outside,
              "refused": len(outside)}
    if not ids:
        return CheckResult("G3", "cited_ids", False, "nothing_cited", None, detail)
    if outside:
        return CheckResult("G3", "cited_ids", False, "not_in_run_slice", None, detail)
    return CheckResult("G3", "cited_ids", True, None, None, detail)


# ---------------------------------------------------------------------------
# G8 — what may support what
# ---------------------------------------------------------------------------


def check_support_row_role(statement: StatementLike, row: SupportLike, row_index: int) -> CheckResult:
    """G8 per row — the CHECK constraints the store will carry, plus the rules
    a constraint cannot see (it needs the cited statement's kind)."""
    role, kind = _role(row), _skind(row)
    if role not in _ROLES:
        return CheckResult("G8", "role", False, "unknown_role", row_index, {"role": role})
    if kind not in _SUPPORT_KINDS:
        return CheckResult("G8", "role", False, "unknown_support_kind", row_index, {"support_kind": kind})
    if kind == SupportKind.STATEMENT.value and role == SupportRole.EVIDENCE.value:
        return CheckResult("G8", "role", False, "statement_is_never_evidence", row_index)
    if kind == SupportKind.FACT.value and role != SupportRole.BACKGROUND.value:
        return CheckResult("G8", "role", False, "fact_is_background_only", row_index)
    if kind == SupportKind.KNOWLEDGE_ITEM.value and role == SupportRole.EVIDENCE.value:
        # Design B1 floor F4: an item may be evidence only when its set says so
        # and it is not derived from this statement's lineage. Neither fact is
        # available to the gate yet, so an item edge may not be evidence.
        return CheckResult("G8", "role", False, "knowledge_item_evidence_undeclared", row_index)
    if kind == SupportKind.STATEMENT.value and role == SupportRole.PREMISE.value:
        ref_kind = enum_value(row.ref_statement_kind)
        if not ref_kind:
            return CheckResult("G8", "role", False, "ref_statement_kind_unknown", row_index)
        if (ref_kind == StatementKind.ASSESSMENT.value
                and enum_value(statement.kind) != StatementKind.ASSESSMENT.value):
            return CheckResult("G8", "role", False, "assessment_never_supports_factual",
                               row_index, {"ref_statement_kind": ref_kind})
    return CheckResult("G8", "role", True, None, row_index)


def check_statement_shape(statement: StatementLike, support: Sequence[SupportLike]) -> list[CheckResult]:
    """G8 statement-level: closed kind / subject vocabularies, at least one
    evidence row (background alone never establishes — design floor F5), at
    most :data:`MAX_PREMISES` premises."""
    out: list[CheckResult] = []
    kind = enum_value(statement.kind)
    sk = enum_value(statement.subject_kind)
    if kind not in _KINDS:
        out.append(CheckResult("G8", "kind", False, "unknown_kind", None, {"kind": kind}))
    if sk not in _SUBJECT_KINDS or not str(statement.subject_key or "").strip():
        out.append(CheckResult("G8", "subject", False, "bad_subject", None,
                               {"subject_kind": sk, "subject_key": statement.subject_key}))
    n_evidence = len(_evidence(support))
    out.append(CheckResult("G8", "evidence_present", n_evidence > 0,
                           None if n_evidence else "no_evidence_row", None,
                           {"evidence_rows": n_evidence}))
    n_premise = sum(1 for r in support if _role(r) == SupportRole.PREMISE.value)
    out.append(CheckResult("G8", "premise_limit", n_premise <= MAX_PREMISES,
                           None if n_premise <= MAX_PREMISES else "too_many_premises", None,
                           {"premises": n_premise, "max": MAX_PREMISES}))
    return out


# ---------------------------------------------------------------------------
# G11 (text half) — no control text carried as memory
# ---------------------------------------------------------------------------


def check_control_text(statement: StatementLike, support: Sequence[SupportLike]) -> CheckResult:
    """The statement text and every quote pass the journal gate's one control
    text list (``journal_evidence.find_control_text``)."""
    hits: dict[str, list[str]] = {}
    found = find_control_text(statement.text)
    if found:
        hits["text"] = found
    for i, r in enumerate(support):
        found = find_control_text(r.quote)
        if found:
            hits[f"row{i}"] = found
    if hits:
        return CheckResult("G11", "control_text", False, "control_text", None, {"hits": hits})
    return CheckResult("G11", "control_text", True)


# ---------------------------------------------------------------------------
# G13 — the access ceiling
# ---------------------------------------------------------------------------


def access_rank(access_class: str | None) -> int | None:
    """Rank in :data:`ACCESS_RANK` (``None`` → undeclared; unknown → ``None``)."""
    value = access_class or UNDECLARED_ACCESS
    return ACCESS_RANK.index(value) if value in ACCESS_RANK else None


def access_ceiling(
    support: Sequence[SupportLike], *, roles: Iterable[str] | None = None,
) -> tuple[str, CheckResult]:
    """G13: the most restrictive access class among the support rows (all
    roles unless ``roles`` narrows it — a contradicting quote is shown beside
    the statement too). An unknown class fails the check and the ceiling is
    the most restrictive class, never a guess downward. No rows → ``public``,
    as the existing rule (``access.access_ceiling_sql``) answers when nothing
    restricts."""
    wanted = set(roles) if roles is not None else None
    ranks: list[int] = []
    unknown: list[str] = []
    for r in support:
        if wanted is not None and _role(r) not in wanted:
            continue
        rank = access_rank(r.access_class)
        if rank is None:
            unknown.append(str(r.access_class))
        else:
            ranks.append(rank)
    if unknown:
        ceiling = ACCESS_RANK[-1]
        return ceiling, CheckResult("G13", "access_ceiling", False, "unknown_access_class",
                                    None, {"unknown": sorted(set(unknown)), "ceiling": ceiling})
    ceiling = ACCESS_RANK[max(ranks)] if ranks else ACCESS_CLASSES[0]
    return ceiling, CheckResult("G13", "access_ceiling", True, None, None, {"ceiling": ceiling})


def ceiling_admits(ceiling: str, reader_max: str) -> bool:
    """May a reader cleared up to ``reader_max`` see a version whose stored
    ceiling is ``ceiling``? Unknown on either side → no."""
    c, r = access_rank(ceiling), access_rank(reader_max)
    return c is not None and r is not None and c <= r


# ---------------------------------------------------------------------------
# All code gates over one proposal
# ---------------------------------------------------------------------------


def evaluate_code_gates(
    statement: StatementLike,
    support: Sequence[SupportLike],
    *,
    texts: EvidenceTexts,
    subject_names: Iterable[str],
    handed: HandedSlice,
    allow_publication_date: bool = True,
    claim_values: Iterable[str] = ("claim",),
    ceiling_roles: Iterable[str] | None = None,
    read_at: datetime | date | None = None,
    check_valid_from_source: bool = True,
    counterpart_names: Iterable[str | SubjectName] | None = None,
) -> GateReport:
    """Run G1, G2, G3, G7, G8, G11 (text), G13 over one proposed statement
    version and its support rows. Never raises on bad input: every defect is
    a failed check with a reason.

    ``read_at`` is the time the desk wrote the sentence (the finding's
    production time): it resolves the statement's own relative day words and
    identifies an as-of frame naming the read's day. ``allow_publication_date``
    and ``check_valid_from_source`` exist so a dry run can rerun the S-3
    configuration (``False`` / ``False``) beside the current rule."""
    claim_values = tuple(claim_values)
    checks: list[CheckResult] = list(check_statement_shape(statement, support))
    for i, row in enumerate(support):
        checks.append(check_support_row_role(statement, row, i))
        checks.append(check_quote(row, texts, row_index=i))
        checks.append(check_handed_row(row, handed, i))
        checks.append(check_claim_source(statement, row, i, claim_values=claim_values))
        if _role(row) == SupportRole.EVIDENCE.value:
            checks.append(check_valid_from(statement, row, i))
    checks.append(check_slots(statement))
    checks.append(check_numbers(statement, support, texts))
    checks.append(check_dates(statement, support, allow_publication_date=allow_publication_date,
                              read_at=read_at))
    if check_valid_from_source:
        checks.append(check_valid_from_stated(statement, support, read_at=read_at))
    checks.append(check_subject_named(support, subject_names))
    if counterpart_names is not None:
        checks.append(check_counterpart_named(support, counterpart_names))
    checks.append(check_control_text(statement, support))
    ceiling, ceiling_check = access_ceiling(support, roles=ceiling_roles)
    checks.append(ceiling_check)
    groups = count_groups(support, texts)
    contra = contradicting_groups(support, texts)
    checks.append(CheckResult("G7", "independence", True, None, None, groups.as_dict()))
    return GateReport(
        checks=tuple(checks),
        independent_groups=groups.groups,
        checkable_groups=groups.checkable_groups,
        contradicting_groups=contra,
        support_word=derive_support_word(groups.groups, contra),
        access_ceiling=ceiling,
    )


def evaluate_assessment_gates(statement: StatementLike, support: Sequence[SupportLike]) -> GateReport:
    """The code gates for an ASSESSMENT — the desk's judgement (S-5). It never
    needs a quote and is never established by one: its support may be ONLY
    premises, each another typed statement (G8 ``assessment_premises``), at
    most :data:`MAX_PREMISES`; kind and subject in vocabulary; no control
    text. A zero-premise assessment passes as the unsupported judgement it is
    and says so (``premises: 0``)."""
    checks: list[CheckResult] = [c for c in check_statement_shape(statement, support)
                                 if c.check != "evidence_present"]
    if enum_value(statement.kind) != StatementKind.ASSESSMENT.value:
        checks.append(CheckResult("G8", "assessment_premises", False, "not_an_assessment", None,
                                  {"kind": enum_value(statement.kind)}))
    bad = [i for i, r in enumerate(support)
           if _role(r) != SupportRole.PREMISE.value or _skind(r) != SupportKind.STATEMENT.value]
    checks.append(CheckResult("G8", "assessment_premises", not bad,
                              "assessment_support_not_premise" if bad else None, None,
                              {"premises": len(support) - len(bad), "not_premise_rows": bad}))
    for i, row in enumerate(support):
        checks.append(check_support_row_role(statement, row, i))
    checks.append(check_control_text(statement, support))
    return GateReport(checks=tuple(checks), access_ceiling=access_ceiling(support)[0])


#: An absence states one: a negation or an absence word ("no signal …",
#: "none of the four …", "carries nothing on …"). A piece typed ``absence``
#: without one (a watch item, a list of what the slice DOES hold) is not an
#: absence and fails ``G8.absence_stated`` — it cannot escape the verifier
#: by its kind.
_ABSENCE_RE = re.compile(
    r"\b(?:no|not|none|nothing|neither|nor|without|absent|absence|lack\w*|nil|zero|never|"
    r"[a-z]+n['’]t)\b",
    re.IGNORECASE,
)


def absence_stated(text: str | None) -> bool:
    """Does ``text`` state an absence (a negation or absence word)?"""
    return bool(text) and bool(_ABSENCE_RE.search(strip_markers(text)))


def absence_scope(handed: HandedSlice) -> dict[str, Any]:
    """An absence's scope, from the run's handed slice (code, never the
    model): which collection, which sources, which window, which run."""
    def _iso(v: Any) -> str | None:
        return v.isoformat() if isinstance(v, (datetime, date)) else None

    return {"collection": getattr(handed, "collection", None),
            "source_ids": sorted(getattr(handed, "source_ids", ()) or ()),
            "window_start": _iso(getattr(handed, "window_start", None)),
            "window_end": _iso(getattr(handed, "window_end", None)),
            "run_id": handed.run_id, "handed_rows": len(handed.ref_ids)}


def evaluate_absence_gates(
    statement: StatementLike, *, handed: HandedSlice, cited: Iterable[Any] = (),
) -> GateReport:
    """The code gates for an ABSENCE (S-5): "no signal in this slice …" is a
    statement about the COLLECTION the run was handed, not about the world.
    It carries no quote. It passes only when the run's handed slice says what
    the collection was — its id and its time window (G8 ``absence_scope``) —
    and every row the piece cites was handed (G3)."""
    checks: list[CheckResult] = [c for c in check_statement_shape(statement, ())
                                 if c.check != "evidence_present"]
    if enum_value(statement.kind) != StatementKind.ABSENCE.value:
        checks.append(CheckResult("G8", "absence_kind", False, "not_an_absence"))
    stated = absence_stated(statement.text)
    checks.append(CheckResult("G8", "absence_stated", stated, None if stated else "absence_not_stated"))
    scope = absence_scope(handed)
    missing = [k for k in ("collection", "window_start", "window_end") if not scope.get(k)]
    checks.append(CheckResult("G8", "absence_scope", not missing,
                              "absence_scope_unknown" if missing else None, None,
                              {**scope, "missing": missing}))
    cited = [c for c in cited if c is not None]
    if cited:
        checks.append(check_cited_ids(cited, handed))
    checks.append(check_control_text(statement, ()))
    return GateReport(checks=tuple(checks), access_ceiling=ACCESS_CLASSES[0])


def tally(reports: Iterable[GateReport]) -> dict[str, dict[str, int]]:
    """Pass/fail counts per ``gate.check`` and per failure reason over many
    reports — the read-only dry run's per-check pass rates (counts only)."""
    passed: Counter[str] = Counter()
    failed: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    proposals = 0
    clean = 0
    for rep in reports:
        proposals += 1
        clean += int(rep.passed)
        for c in rep.checks:
            key = f"{c.gate}.{c.check}"
            if c.passed:
                passed[key] += 1
            else:
                failed[key] += 1
                reasons[f"{key}:{c.reason}"] += 1
    return {
        "totals": {"proposals": proposals, "passed_all": clean},
        "passed": dict(passed),
        "failed": dict(failed),
        "reasons": dict(reasons),
    }


def report_rows(report: GateReport, n_rows: int) -> list[list[Mapping[str, Any]]]:
    """Per support row, its ``checks`` jsonb (same order as the rows)."""
    return [report.row_checks(i) for i in range(n_rows)]


__all__ = [
    "ACCESS_RANK",
    "absence_scope",
    "absence_stated",
    "check_counterpart_named",
    "evaluate_absence_gates",
    "evaluate_assessment_gates",
    "feed_item_texts",
    "locate_quote",
    "MAX_PREMISES",
    "MIN_QUOTE_WORDS",
    "QUOTED_ROLES",
    "SLICE_KINDS",
    "access_ceiling",
    "access_rank",
    "ceiling_admits",
    "check_cited_ids",
    "check_claim_source",
    "check_control_text",
    "check_dates",
    "check_handed_row",
    "check_numbers",
    "check_quote",
    "check_slots",
    "check_statement_shape",
    "check_subject_named",
    "check_support_row_role",
    "check_valid_from",
    "check_valid_from_stated",
    "evaluate_code_gates",
    "report_rows",
    "resolve_text",
    "tally",
]
