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
* ``align_verdicts`` / ``partial_verdict_budget`` (2026-09-20/1) — WHICH claim
  each returned verdict belongs to. A full-length response aligns by position
  exactly as it always did; a SHORT one aligns only by the claim number its
  entries name, and the claims nobody named are left ``_VERDICT_UNCHECKED``
  rather than guessed at. See the block above them for the whole argument.
  ``align_verdicts`` also returns ``aligned_by`` (H3, 2026-09-25/1) —
  :data:`ALIGNED_BY_CLAIM_INDEX` or :data:`ALIGNED_BY_POSITIONAL` — so the
  caller can persist HOW a verdict was matched to its claim, not just that it
  was.
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


# ---------------------------------------------------------------------------
# PARTIAL VERDICT SETS (2026-09-20/1) — the ALIGNMENT contract
# ---------------------------------------------------------------------------
# THE DEFECT, measured live. ~1.5% of judge calls end with
# ``verify.faithfulness.judge_failed err=judge returned 22 verdicts for 23
# claims`` (15 on 2026-09-19, 4 on 2026-09-20; also 73 for 74) — the model drops
# ONE verdict out of twenty-odd and the WHOLE row falls to the deterministic
# floor, labelled ``judge-unavailable:judge_error`` and capped at the
# PROVISIONAL 0.85 ceiling. Twenty-two adjudicated claims are discarded to
# punish one missing one, which is the same amplifier PARTITION-PRESERVE
# (2026-09-08/1) removed one level up.
#
# WHAT IS AND IS NOT SALVAGEABLE, and the line is IDENTITY. A verdict list is a
# POSITIONAL protocol: ``{"verdicts": [...]}``, one token per numbered claim, in
# order. When an entry carries no claim identity and one is missing, there is no
# way to know WHICH claim went ungraded — every verdict after the drop point may
# belong to the claim before it — so a positional short list is not a partial
# answer, it is an unreadable one, and it keeps today's hard failure.
#
# What IS readable is a response whose entries NAME their claim: the judge is
# shown ``1. <claim>`` … ``N. <claim>`` and may answer with that number attached
# (``{"claim": 3, "verdict": "supported"}``, or a parallel ``"claim_indices"``
# array). Those align by ID, never by position, and the claims no entry names
# are left UNCHECKED — not unsupported, not supported, not graded at all. An
# unchecked claim leaves the judged population exactly the way a floored
# partition's claims already do (``residual_floor_spans`` folds in whatever the
# DETERMINISTIC floor found about it; ``carried_ledger`` carries its floor row),
# so no verdict is ever fabricated for a claim nobody graded.
#
# AND IT IS BOUNDED. A response missing more than
# :func:`partial_verdict_budget` of its verdicts is not a judge that skipped a
# line, it is a judge that answered a different question — that keeps today's
# failure too.
#
# The ID arm also repairs a silent defect of its own: an entry that arrived as
# ``{"claim": 3, "verdict": "supported"}`` at the RIGHT length used to be
# stringified whole (``str({...})``), miss the four-token vocabulary, and coerce
# to ``unsupported``. A labelled verdict is now read as the verdict it is.
#
# 2026-09-24/1 (H3) arms this arm from the PROMPT side: the reply contract in
# ``judge_quote_rules._judge_reply_contract`` asks every verdict entry to name
# its claim as ``claim_index``, so the machinery below is what the judge is
# actually asked for — before that stamp it fired only when a model volunteered
# ids unprompted.

#: The slot a graded claim carries when the judge returned NO verdict for it.
#: NOT a grade: it is neither in the numerator nor in the judged denominator,
#: and it never reaches the severity chain or the ledger.
_VERDICT_UNCHECKED = "unchecked"

#: H3 (2026-09-25/1) — HOW :func:`align_verdicts` matched a verdict to its
#: claim, persisted on every ledger row so the id arm (armed by
#: ``2026-09-20/1``, finally ASKED FOR by the ``2026-09-24/1`` reply contract)
#: can be MEASURED rather than merely trusted to have fired. Exactly one value
#: per call: the two branches below are mutually exclusive (either every entry
#: named a distinct claim, or none did and the full-length list zipped
#: positionally), so there is no response that mixes the two.
ALIGNED_BY_CLAIM_INDEX = "claim_index"
ALIGNED_BY_POSITIONAL = "positional"


def partial_verdict_budget(n_claims: int) -> int:
    """How many verdicts ONE response may drop and still be read as partial.

    ``max(2, ceil(0.1 * N))`` — two is the floor because a 3-claim partition
    dropping one is the same clerical slip a 30-claim partition dropping three
    is, and 10% is the ceiling because a response missing more than a tenth of
    its answers is not a slip.
    """
    return max(2, -(-max(0, int(n_claims)) // 10))


#: Keys a judge may hang the CLAIM NUMBER off. Read in order; a key that is
#: present but not a claim ordinal (``{"claim": "<the claim text>"}``) is
#: skipped rather than failing the entry.
_VERDICT_ID_KEYS: tuple[str, ...] = (
    "claim", "claim_index", "claim_id", "claim_no", "claim_number",
    "index", "idx", "number", "no", "n", "id", "i",
)
#: Keys carrying the VERDICT token itself on an object-form entry.
_VERDICT_VALUE_KEYS: tuple[str, ...] = (
    "verdict", "value", "label", "grade", "result", "v",
)
#: Keys carrying an entry's OWN evidence quote (V-D), which wins over the
#: parallel ``quotes`` array for that claim — it cannot be misaligned.
_VERDICT_QUOTE_KEYS: tuple[str, ...] = ("quote", "evidence", "span")
#: Top-level arrays parallel to ``verdicts`` that name each entry's claim.
_VERDICT_ID_SIDECARS: tuple[str, ...] = (
    "claim_indices", "claim_ids", "claim_numbers", "claims", "indices",
)

#: A claim ordinal as a judge may write it: ``3``, ``"3"``, ``"3."``, ``"#3"``.
_CLAIM_ORDINAL_RE = re.compile(r"^[#\s]*(\d{1,4})\s*[.):\]]?$")


def _claim_ordinal(value: Any, n_claims: int) -> int | None:
    """The 1-based claim number in ``value`` as a 0-based index, or ``None``.

    ``None`` for anything that is not an in-range ordinal — a claim's TEXT, a
    float, a bool, an out-of-range number — so a mis-read can only ever cost the
    salvage, never mis-attribute a verdict.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        n = value
    elif isinstance(value, str):
        m = _CLAIM_ORDINAL_RE.match(value.strip())
        if not m:
            return None
        n = int(m.group(1))
    else:
        return None
    return n - 1 if 1 <= n <= n_claims else None


def _entry_verdict(entry: Any) -> Any:
    """The raw verdict token of one entry — the entry itself, or its field."""
    if isinstance(entry, dict):
        for key in _VERDICT_VALUE_KEYS:
            if key in entry:
                return entry[key]
        return ""
    return entry


def _entry_quote(entry: Any) -> str | None:
    """An object-form entry's OWN quote, or ``None`` to use the parallel array."""
    if isinstance(entry, dict):
        for key in _VERDICT_QUOTE_KEYS:
            quote = entry.get(key)
            if isinstance(quote, str):
                return quote
    return None


def _entry_index(entry: Any, n_claims: int) -> int | None:
    """The claim this entry NAMES, 0-based, or ``None`` when it names none."""
    if not isinstance(entry, dict):
        return None
    for key in _VERDICT_ID_KEYS:
        if key in entry:
            idx = _claim_ordinal(entry[key], n_claims)
            if idx is not None:
                return idx
    return None


def _verdict_claim_indices(
    raw: list[Any], parsed: dict[str, Any], n_claims: int
) -> list[int] | None:
    """One claim index per entry, or ``None`` when identity is not complete.

    ALL-OR-NOTHING by design, twice over: every entry must name a claim (a
    half-labelled list is ambiguous exactly where it matters), and the names
    must be DISTINCT (two verdicts for one claim is not an alignment, it is a
    contradiction). Either failure returns ``None`` and the caller falls back to
    the strict positional contract.
    """
    if not raw:
        return None
    per_entry = [_entry_index(entry, n_claims) for entry in raw]
    idx: list[int] | None = None
    if all(i is not None for i in per_entry):
        idx = [int(i) for i in per_entry]  # type: ignore[arg-type]
    else:
        for key in _VERDICT_ID_SIDECARS:
            sidecar = parsed.get(key)
            if not isinstance(sidecar, list) or len(sidecar) != len(raw):
                continue
            cand = [_claim_ordinal(v, n_claims) for v in sidecar]
            if all(c is not None for c in cand):
                idx = [int(c) for c in cand]  # type: ignore[arg-type]
                break
    if idx is None or len(set(idx)) != len(idx):
        return None
    return idx


def alignment_audit_fields(
    claim_verdicts: list[Any], miscount_claims: int, judge_partial: int | None
) -> dict[str, int]:
    """H3 (2026-09-25/1) — the ``verification`` block's alignment-AUDIT
    fragment: how many persisted ledger rows :func:`align_verdicts` matched by
    id vs by position, the reply-count mismatch summed across every judge
    partition, and the claims a NAMED reply never named at all — the same
    population ``judge_partial`` already tracks. All four are 0 on a
    floor-only pass: no fold outside the judge path ever sets ``aligned_by``.
    """
    return {
        "miscount_claims": miscount_claims,
        "aligned_by_id": sum(
            getattr(cv, "aligned_by", None) == ALIGNED_BY_CLAIM_INDEX
            for cv in claim_verdicts
        ),
        "aligned_positionally": sum(
            getattr(cv, "aligned_by", None) == ALIGNED_BY_POSITIONAL
            for cv in claim_verdicts
        ),
        "unmatched_claims": judge_partial or 0,
    }


def align_verdicts(
    raw: list[Any], parsed: dict[str, Any], n_claims: int
) -> tuple[list[tuple[Any, str] | None], list[int], str]:
    """``(slots, missing, aligned_by)`` — one slot per claim, in claim order.

    ``slots[i]`` is ``(raw verdict, quote)`` for every claim the judge graded and
    ``None`` for every claim it did not; ``missing`` lists the ``None`` ones
    (0-based). A full-length positional response — the healthy case and the
    overwhelming majority — yields the same pairs the pre-2026-09-20 ``zip``
    produced, in the same order.

    ``aligned_by`` (H3, 2026-09-25/1) is :data:`ALIGNED_BY_CLAIM_INDEX` when
    every entry named a distinct claim (the id arm decided ``ids``) and
    :data:`ALIGNED_BY_POSITIONAL` for the full-length positional fallback —
    the caller stamps it onto every ledger row this call produces, so HOW a
    verdict was matched to its claim is a persisted fact, not an inference.

    Raises :class:`_JudgeVerdictError`, exactly as before, when the count does
    not match AND the response carries no usable claim identity, and when a
    labelled response drops more than :func:`partial_verdict_budget` verdicts.
    """
    ids = _verdict_claim_indices(raw, parsed, n_claims)
    if ids is None:
        if len(raw) != n_claims:
            raise _JudgeVerdictError(
                f"judge returned {len(raw)} verdicts for {n_claims} claims"
            )
        ids = list(range(n_claims))
        aligned_by = ALIGNED_BY_POSITIONAL
    else:
        dropped = n_claims - len(raw)
        budget = partial_verdict_budget(n_claims)
        if dropped > budget:
            raise _JudgeVerdictError(
                f"judge returned {len(raw)} verdicts for {n_claims} claims "
                f"— {dropped} unchecked is over the partial budget of {budget}"
            )
        aligned_by = ALIGNED_BY_CLAIM_INDEX
    # V-D: the parallel quote array, unchanged — honoured only at the length of
    # the verdict list it parallels, and overridden per-entry by an object-form
    # entry's own quote (which cannot be misaligned).
    quotes_raw = parsed.get("quotes")
    quotes: list[str] = (
        [q if isinstance(q, str) else "" for q in quotes_raw]
        if isinstance(quotes_raw, list) and len(quotes_raw) == len(raw)
        else [""] * len(raw)
    )
    slots: list[tuple[Any, str] | None] = [None] * n_claims
    for entry, quote, i in zip(raw, quotes, ids):
        own = _entry_quote(entry)
        slots[i] = (_entry_verdict(entry), own if own is not None else quote)
    return slots, [i for i, slot in enumerate(slots) if slot is None], aligned_by
