# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Judge verdict parsing — the judge subsystem's next brick.

Everything here answers the mechanical half of "what did the judge say?",
never the substantive half of "was it right?" (that stays in ``verify``,
beside the report/ledger types and the severity table it must agree with).

* ``_extract_json_objects`` — pulls every balanced top-level JSON object out
  of a judge response, fence- and prose-tolerant, so a reasoning-class judge
  that thinks out loud before its strict-JSON verdicts still parses (#116d).
* ``_JudgeVerdictError`` — the shape a malformed verdict set takes: raised so
  the caller fails to the deterministic floor rather than silently
  zip-truncating a partial pass that hides ungraded claims.
* ``_judge_reason`` / ``_judge_detail`` — the ONE mapping from a raw verdict
  token to its span/ledger REASON and its persisted evidence-quote DETAIL
  (W2), shared by ``unsupported_spans`` and ``claim_verdicts`` so the two
  arms can never disagree about a claim's class again.
* ``_is_uncited_world_baseline`` (V-G5) rides along as the smallest adjacent
  self-contained helper: a pure predicate over claim text — no report/ledger
  coupling — that sat directly beside this cluster in ``verify``. It answers
  whether a MARKERLESS claim rests on a world baseline no cited row supplies,
  which is where the judge's "no marker ⇒ synthesis" licence applies.

``verify`` imports these ONE WAY and re-exports every name, so
``verify._extract_json_objects``, ``verify._JudgeVerdictError``,
``verify._judge_reason``, ``verify._judge_detail`` and
``verify._is_uncited_world_baseline`` resolve exactly as before. Extracted
2026-08-27 when the V-I precision train pushed verify.py past its
DO-NOT-RAISE ceiling; the seam was already named in the file's own
history — parsing what the judge SAID, decided in one place, next to the
constants (``judge_quote_rules`` / ``judge_absence_rubric``) whose verdict
vocabulary it reads. The FOLD (``_fold_markerless_uncited``) stays in
``verify``: it manipulates the report/ledger types this module does not own.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .absence_slice import hedged_conflict_disclosure
from .judge_absence_rubric import _JUDGE_NONPROP_UNEARNED, _VERDICT_NONPROP_UNEARNED
from .judge_quote_rules import (
    _JUDGE_CONTRADICTED_HEDGED,
    _JUDGE_CONTRADICTED_MACHINE_ROW,
    _JUDGE_CONTRADICTED_OFF_SCOPE,
    _JUDGE_CONTRADICTED_ROUTE_EXCLUDED,
    _JUDGE_CONTRADICTED_UNQUOTED,
    _JUDGE_CONTRADICTED_UNREFUTED,
    _JUDGE_PRIOR_READ_CONFLICT,
    _JUDGE_QUOTE_CONFIRMS,
    _VERDICT_CONTRADICTED_HEDGED,
    _VERDICT_CONTRADICTED_MACHINE_ROW,
    _VERDICT_CONTRADICTED_OFF_SCOPE,
    _VERDICT_CONTRADICTED_UNQUOTED,
    _VERDICT_CONTRADICTED_UNREFUTED,
    _VERDICT_PRIOR_READ_CONFLICT,
    _VERDICT_QUOTE_CONFIRMS,
    _VERDICT_ROUTE_EXCLUDED,
)


class _JudgeVerdictError(RuntimeError):
    """The judge returned a structurally-invalid verdict set — a verdict count
    that does not match the graded claims. Raised so :func:`_maybe_llm_judge`
    fails to the deterministic floor labelled ``judge_error`` (#116d), rather than
    silently zip-truncating to a partial pass that hides ungraded claims."""


def _extract_json_objects(text: str) -> list[dict[str, Any]]:
    """Every balanced top-level ``{...}`` block in ``text`` that parses as a JSON
    dict, in order (#116d).

    Fence- and prose-tolerant: a ```` ```json ```` (or bare ```` ``` ````) fence is
    unwrapped first, and leading reasoning prose / trailing text around the object
    are ignored — a reasoning-class judge may emit thinking before the strict-JSON
    verdicts. Returns ``[]`` when nothing parses. The caller picks the object that
    actually carries ``verdicts`` (so a stray brace in prose can't shadow it).
    """
    if not text:
        return []
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*\n?(.*?)```", candidate, re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1).strip()
    objs: list[dict[str, Any]] = []
    i, n = 0, len(candidate)
    while i < n:
        if candidate[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        escaped = False
        end: int | None = None
        j = i
        while j < n:
            ch = candidate[j]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
            j += 1
        if end is None:
            break  # unbalanced tail — nothing complete left to extract
        try:
            obj = json.loads(candidate[i:end])
            if isinstance(obj, dict):
                objs.append(obj)
        except (json.JSONDecodeError, ValueError):
            pass
        i = end
    return objs


#: How much of an earned evidence quote is persisted onto the verdict.
_JUDGE_QUOTE_DETAIL_CHARS = 300


def _judge_reason(verdict: str) -> str:
    """The span/ledger REASON for one judge verdict — the ONE mapping.

    Shared by ``unsupported_spans`` and ``claim_verdicts`` so the two can never
    disagree about a claim's class again (W2: the ledger arm used to collapse
    both demotion labels back to ``judge_unsupported``).

    RUST-3: an EARNED ``not_a_proposition`` never reaches this function — it is
    filtered out of the graded population upstream, so there is no reason to map
    and no failure to name. Only the WITHDRAWN form has a reason.
    """
    if verdict == _VERDICT_NONPROP_UNEARNED:
        return _JUDGE_NONPROP_UNEARNED
    if verdict == "contradicted":
        return "judge_contradicted"
    if verdict == _VERDICT_CONTRADICTED_UNQUOTED:
        return _JUDGE_CONTRADICTED_UNQUOTED
    if verdict == _VERDICT_CONTRADICTED_UNREFUTED:
        return _JUDGE_CONTRADICTED_UNREFUTED
    if verdict == _VERDICT_PRIOR_READ_CONFLICT:
        return _JUDGE_PRIOR_READ_CONFLICT
    if verdict == _VERDICT_CONTRADICTED_OFF_SCOPE:
        return _JUDGE_CONTRADICTED_OFF_SCOPE
    if verdict == _VERDICT_QUOTE_CONFIRMS:
        return _JUDGE_QUOTE_CONFIRMS
    if verdict == _VERDICT_CONTRADICTED_MACHINE_ROW:
        return _JUDGE_CONTRADICTED_MACHINE_ROW
    if verdict == _VERDICT_CONTRADICTED_HEDGED:
        return _JUDGE_CONTRADICTED_HEDGED
    if verdict == _VERDICT_ROUTE_EXCLUDED:
        return _JUDGE_CONTRADICTED_ROUTE_EXCLUDED
    return "judge_unsupported"


def _judge_detail(verdict: str, quote: str, claim: str = "") -> str | None:
    """The persisted WHY for a judge verdict — the earned evidence quote (W2).

    A hard fail that cannot show its refutation is a hard fail nobody can audit:
    the quote was computed, used for the severity decision, and thrown away.
    ``None`` for every verdict that carries no earned quote, so the ledger row is
    byte-identical for them.

    ``claim`` (V-J1, 2026-08-28) is OPTIONAL and read by exactly one branch: the
    hedged-conflict demotion, whose whole earned-detail argument is that the
    CLAIM names both poles. Defaulted so every caller that does not have the
    text — and every historical one — is byte-identical.
    """
    if not quote:
        return None
    span = re.sub(r"\s+", " ", str(quote)).strip()[:_JUDGE_QUOTE_DETAIL_CHARS]
    if verdict == _VERDICT_CONTRADICTED_UNREFUTED:
        return (
            "the judge's evidence span RESOLVES the claim's subject without "
            f"refuting it, so the hard class was not earned: {span!r}"
        )
    if verdict == _VERDICT_PRIOR_READ_CONFLICT:
        return (
            "this read CONFLICTS with an analyst finding the claim does not cite "
            "(typically this desk's own prior read) rather than with source "
            f"reporting — an update, not a misstatement of evidence: {span!r}"
        )
    if verdict == _VERDICT_CONTRADICTED_OFF_SCOPE:
        return (
            "the claim ENUMERATED what it denies and the judge's evidence span "
            "names none of those things in full, so it evidences something the "
            f"claim never denied: {span!r}"
        )
    if verdict == _VERDICT_QUOTE_CONFIRMS:
        return (
            "the judge's evidence span states the claim's OWN numbers back to "
            "it under numeral/unit normalization ('16' for 'sixteen'), every "
            "pinned clock/date endpoint matching and no prose direction "
            f"opposing — it CONFIRMS and cannot be what refutes it: {span!r}"
        )
    if verdict == _VERDICT_CONTRADICTED_MACHINE_ROW:
        return (
            "the judge's evidence span resolves ONLY inside a GDELT/CAMEO "
            "machine-coded event record — a machine's reading of an article, "
            "not the article's own words, and the class the V-B route already "
            f"excludes: {span!r}"
        )
    if verdict == _VERDICT_CONTRADICTED_HEDGED:
        # V-J1: the guard's OWN detail names both poles verbatim off the claim
        # (the V-D earned-severity rule). A caller that passed no claim text
        # falls back to the mechanism stated plainly — never to nothing.
        why = hedged_conflict_disclosure(claim) or (
            "the claim DISCLOSED this pole and marked it WEAK, preferring the "
            "verified one it named alongside it; a sentence is not refuted by "
            "the side it already rejected"
        )
        return f"{why}: {span!r}"
    if verdict == _VERDICT_ROUTE_EXCLUDED:
        return (
            "the V-B router had already routed this claim OUT of slice checking "
            "as a continuity / volume / trajectory read; one claim cannot have "
            f"two authorities, so the hard class was not available: {span!r}"
        )
    return f"contradicted by a verbatim evidence span: {span!r}"


#: A HISTORICAL / STRUCTURAL BASELINE about the world — the load-bearing premise
#: shape. Narrow idioms only; every one of these asserts a fact about how things
#: have been, which no row in a 24-hour signal slice reports.
_WORLD_BASELINE_RE = re.compile(
    r"\bhistorical(?:ly)?\b"
    r"|\blong[-\s]standing\b|\blongstanding\b"
    r"|\btraditionally\b"
    r"|\bchronic(?:ally)?\b"
    r"|\bpropensity\b"
    r"|\btrack\s+record\b"
    r"|\bhistory\s+of\b|\bhas\s+a\s+history\b"
    r"|\bwell[-\s]documented\b"
    r"|\bknown\s+for\b"
    r"|\bendemic\b|\bperennial\b"
    r"|\bpast\s+pattern"
    r"|\bbase\s+rate\b"
    r"|\breference\s+class\b",
    re.IGNORECASE,
)

#: The baseline is about the EVIDENCE SET, not the world — the absence
#: machinery's territory, and no world claim at all.
_EVIDENCE_REFERENT_RE = re.compile(
    r"\b(?:from|in|within|across|among)\s+(?:the\s+)?"
    r"(?:current\s+|available\s+|collected\s+|reviewed\s+|examined\s+)?"
    r"(?:evidence|signals?|reporting|corpus|documents?|sources?|record\s+set)\b",
    re.IGNORECASE,
)


def _is_uncited_world_baseline(claim: str) -> bool:
    """V-G5 — does this markerless claim rest on an UNCITED world baseline?

    Marker-agnostic by design: the caller supplies only claims that carry no
    citation marker at all, which is where the judge's "no marker ⇒ synthesis"
    licence applies and where the truthmaker therefore cannot be checked.
    """
    core = re.sub(r"[*_`]+", "", claim.strip().lstrip("#-*> ").strip())
    if not _WORLD_BASELINE_RE.search(core):
        return False
    # NOTE the exemption is deliberately NOT ``_has_collection_scope``. That
    # lexicon answers a DIFFERENT question — how the claim's NEGATIVE is scoped —
    # and it is generous by design (it matches bare "window"). A claim can be
    # perfectly scoped to the collection window and still open with a world
    # baseline the analyst supplied from memory, which is the shape under test.
    # Only a baseline that names the EVIDENCE SET as its referent is exempt.
    return not _EVIDENCE_REFERENT_RE.search(core)
