# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G1 — FREEZE and SEGMENT: the live heads become gradable claim atoms.

A straight port of ``planning/PROGRAM1_2026-09-16/freeze_step2_heads.py``'s
pure half (the halves that touch the DB live in the handler). Semantics are
carried, not reinvented — this module's whole job is that a claim the hand-run
kept is a claim the job keeps, and a span the hand-run excluded is excluded here
under the SAME NAMED REASON.

THE SEGMENTER IS THE SHIPPED ONE. ``verify._segment_claims`` +
``verify._is_judgeable_claim``, the pair the faithfulness judge already runs
over every desk and composition head. A second segmenter here would make the
published number a measurement of two segmenters.

THE COMPOSITION GOES THROUGH THE REDACTOR FIRST. A live ``assembly.v1``
composition render carries per-block attribution telemetry (``· verify 0.36 ·
cited mass 3.85``) and two bookkeeping sections. Those are the platform's own
numbers about itself; a world reference cannot bear on them, and shipping them
to an external grader leaks the arm. ``redact_render`` is R4's own redactor
(``build_r4_packets.redact_render``), carried verbatim, and the sections it cuts
are replaced by an HONEST NOTICE rather than deleted silently.

THE EXCLUSIONS, and why each is named rather than merely applied:

  * ``machine_line_registered_shape`` / ``machine_line_middot_fields_with_
    machine_tell`` / ``under_40_chars`` — step 1's registered rules
    (``build_p1_sample.exclusion_reason``);
  * ``not_judgeable_span`` — the shipped segmenter's own hygiene (headings, the
    as-of line, forward-looking watch spans, sub-two-word fragments);
  * ``heading``, ``round_notice`` — structure and the redactor's own notice,
    the latter DERIVED from the constant rather than retyped;
  * ``roster_enumeration_*`` — "Verified reads: …" and bare dimension lists.
    VERDICT_P1v4 caught a family grading the tail of one. An absence claim that
    NAMES dimensions ("leadership_transition … showed no material change") is a
    CLAIM under ANNEX C v4 rule 3 and is KEPT — pinned by a test;
  * ``read_bookkeeping_not_world:<tell>`` — the assembly talking about its own
    construction. A grader with a world reference can only ever call these
    ``silent``, so counting them would dilute the unit's coverage share with
    spans no reference could possibly bear on;
  * ``under_40_chars_after_markers`` — a span only long enough because of its
    citation markers is not a 40-character claim;
  * ``prior_relative:<pattern>`` — the assertion is about this read's relation
    to its OWN previous read ("No material change since the prior read"). The
    reference is a window of world developments and has no prior read, so a
    grader can only ever mislabel it. Anchored on the prior-read REFERENCE and
    never on the negation: "no new disruptions" is a world absence claim, ANNEX
    C v4 rule 3 makes it a claim, and it stays gradeable.

Every excluded span is returned WITH its reason and its text. An exclusion
nobody can audit is an exclusion nobody should trust.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Callable, Mapping, Sequence

#: Grain names and their id prefixes. DISTINCT by construction — the whole
#: reason the two grains can be scored side by side without ever pooling.
GRAIN_DESK = "desk"
GRAIN_COMPOSITION = "composition"
GRAIN_PREFIX: dict[str, str] = {GRAIN_DESK: "DR", GRAIN_COMPOSITION: "CR"}

#: PREREG_P1 §3's floor, carried: a span under this many characters carries no
#: assertion worth grading.
MIN_ASSERTION_CHARS = 40


class SegmentationError(ValueError):
    """A span or an id this module refuses to ship. Loud, never a silent drop."""


# ---------------------------------------------------------------------------
# Identifier normalisation + atom ids
# ---------------------------------------------------------------------------


def norm(text: str) -> str:
    """Whitespace collapsed, casefolded — the harness's id convention.

    Deliberately NOT ``provenance.text_fold``: this is an IDENTIFIER INPUT, not
    a comparator (``r4_common.norm``'s own words). Swapping it would silently
    re-key every atom a prior run wrote, which is how an idempotence guard dies.
    """
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


def atom_id(grain: str, analyst_id: str, created_at: str, text: str) -> str:
    """``DR-``/``CR-`` + first 8 hex of ``sha256(analyst_id|created_at|norm(text))``.

    Reproducible from the frozen head record alone, so a re-run over the same
    head mints the same ids and the per-claim rows are stable across replays.
    """
    if grain not in GRAIN_PREFIX:
        raise SegmentationError(
            f"unknown grain {grain!r} — refusing to mint an id into a namespace "
            f"this module does not register (one of {sorted(GRAIN_PREFIX)})"
        )
    digest = hashlib.sha256(
        f"{analyst_id}|{created_at}|{norm(text)}".encode("utf-8")
    ).hexdigest()
    return f"{GRAIN_PREFIX[grain]}-{digest[:8]}"


# ---------------------------------------------------------------------------
# Citation markers — recorded on the atom, stripped from what the grader sees
# ---------------------------------------------------------------------------

#: ``[[ref:2]]`` (the composition's marker) and ``[32]`` / ``[32][105]`` /
#: ``[32, 105]`` (the desks'). Both are pure provenance: ANNEX C v4 rule 2 says
#: citation markers are not part of the core claim and never decide the label,
#: so a grader that never sees them cannot be tempted by them.
_MARKER_RE = re.compile(r"\[\[ref:\d+\]\]|\[\s*\d+(?:\s*[,;]\s*\d+)*\s*\]")
#: Leading render decoration: the blockquote marker the composition wraps every
#: carried desk line in, and the bullet the desks open list items with.
_DECORATION_RE = re.compile(r"^(?:\s*(?:>+|[-*+])\s+)+")
#: Space (incl. the narrow no-break space the renders use INSIDE words such as
#: "SPICE 1000") left stranded in front of punctuation once a marker is cut.
_SPACE_BEFORE_PUNCT_RE = re.compile(r"[ \t  ]+([.,;:!?])")
_SPACE_RUN_RE = re.compile(r"[ \t  ]{2,}")


def strip_markers(text: str) -> tuple[str, list[str]]:
    """``(the text a grader sees, the markers that were removed)``.

    Only the markers and the leading decoration go. Nothing is re-worded, and a
    LONE U+202F inside a word is preserved — only runs of two or more spaces are
    collapsed — because the renders use it as a digit separator and rewriting it
    would edit the claim, not clean it.
    """
    src = text or ""
    markers = _MARKER_RE.findall(src)
    out = _MARKER_RE.sub("", src)
    out = _DECORATION_RE.sub("", out)
    out = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", out)
    out = _SPACE_RUN_RE.sub(" ", out)
    return out.strip(), markers


# ---------------------------------------------------------------------------
# The redactor — R4's own (build_r4_packets.redact_render), carried verbatim
# ---------------------------------------------------------------------------

_REDACT_ATTRIBUTION: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\s*·\s*verify\s+(?:n/a|[0-9.]+)"), ""),
    (re.compile(r"\s*·\s*cited mass\s+(?:n/a|[0-9.]+)"), ""),
    (re.compile(r"\s*·\s*below floor\b"), ""),
)
_REDACT_SECTIONS = (
    "## Not carried",
    "## What this read did not see",
    "## Coverage",
)
REDACTION_NOTICE = (
    "[SECTION WITHHELD BY THE ROUND — this read's own bookkeeping about which "
    "inputs it did and did not use is withheld from grading by design, exactly "
    "as its evidence trail is. Grade what the read ASSERTS about the world.]"
)


def redact_render(body: str) -> tuple[str, dict[str, Any]]:
    """The rendered read, minus the verify/selection telemetry, plus what was cut."""
    cuts: dict[str, Any] = {"attribution_fields": 0, "sections": []}
    out_lines: list[str] = []
    skipping: str | None = None
    for line in (body or "").split("\n"):
        stripped = line.strip()
        if stripped.startswith("## "):
            if stripped in _REDACT_SECTIONS:
                skipping = stripped
                cuts["sections"].append(stripped)
                out_lines.extend([line, "", REDACTION_NOTICE, ""])
                continue
            skipping = None
        if skipping is not None:
            continue
        new = line
        for pat, repl in _REDACT_ATTRIBUTION:
            new, n_sub = pat.subn(repl, new)
            cuts["attribution_fields"] += n_sub
        out_lines.append(new)
    text = "\n".join(out_lines)
    text, n_stamp = re.subn(r";\s*\d+\s+shown and not carried", "", text)
    cuts["stamp_drop_counts"] = n_stamp
    return text, cuts


# ---------------------------------------------------------------------------
# The exclusions
# ---------------------------------------------------------------------------

#: PREREG_P1 §3's registered machine-line shape, verbatim:
#: ``· <dimension> · <target> · <timestamp> · severity …``
_MACHINE_STRICT_RE = re.compile(
    r"·[^·]{1,80}·[^·]{1,120}·[^·]{1,120}·\s*severity\b", re.IGNORECASE
)
#: The same line with its fields shuffled or one missing. Three or more middot
#: separators PLUS a machine tell is not prose, whatever order it came out in.
_MACHINE_TELL_RE = re.compile(
    r"(\bseverity\b|\d{4}-\d{2}-\d{2}T\d{2}:\d{2})", re.IGNORECASE
)

#: "Verified reads: leadership_transition [[ref:2]], …" — the exact line
#: VERDICT_P1v4 caught a family grading the tail of.
_ROSTER_PREFIX_RE = re.compile(
    r"^\s*(?:verified\s+reads|reads?\s+carried|carried\s+reads|coverage)\s*:",
    re.IGNORECASE,
)
_ROSTER_ALL_RE = re.compile(
    r"\ball\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"verified\s+reads\b",
    re.IGNORECASE,
)
#: Words that may sit around a list of dimension names without making it prose.
#: Deliberately TINY: the moment a roster line carries a verb of its own
#: ("leadership_transition … showed no material change") it is an ABSENCE
#: CLAIM, which ANNEX C v4 rule 3 makes a claim, and it must be graded.
_ROSTER_FILLER = frozenset({
    "verified", "reads", "read", "and", "or", "the", "a", "an", "of", "for",
    "in", "at", "with", "plus", "also", "as", "well", "carried", "coverage",
    "dimensions", "dimension", "desks", "desk", "ref", "refs",
})

#: The assembly's own process bookkeeping — the read talking about ITS OWN
#: construction, never about the country.
_BOOKKEEPING_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pairs examined", re.compile(r"\bpairs\s+examined\b", re.IGNORECASE)),
    ("conflicting pair", re.compile(r"\bconflicting\s+pair\b", re.IGNORECASE)),
    ("shown blocks", re.compile(r"\bshown\s+blocks\b", re.IGNORECASE)),
    ("shown and not carried",
     re.compile(r"\bshown\s+and\s+not\s+carried\b", re.IGNORECASE)),
    ("assembled from N verified",
     re.compile(r"\bassembled\s+from\s+\d+\s+verified\b", re.IGNORECASE)),
    ("BLUF-grain", re.compile(r"\bBLUF-grain\b", re.IGNORECASE)),
)

#: ---------------------------------------------------------------------------
#: PRIOR-RELATIVE CLAIMS — the class no world reference can ever bear on
#: ---------------------------------------------------------------------------
#:
#: A desk read is written AGAINST ITS OWN PREVIOUS READ. So it says things like
#: "No material change since the prior read", "unchanged from the previous
#: assessment", "the pressure stays moderate as in the prior read". The
#: assertion in those spans is not about the country at all — it is about the
#: RELATION between this read and the last one.
#:
#: The reference has no prior read. It is a list of developments in a window,
#: built blind to the substrate, and it cannot possibly say whether this read
#: differs from the one before it. A grader handed such a claim and a reference
#: that DOES carry developments reads the developments as "things happened" and
#: calls the claim ``contradicts`` under ANNEX C v4 rule 3 — which is exactly
#: right for an absence claim about the WORLD and exactly wrong here. Live on
#: 2026-09-20 that is what happened: 13 of the 48 ``contradicts`` labels in
#: ``unit_correctness_claims`` sat on spans of this shape, and TWO Brazil desks
#: (leadership_transition, energy_security) each took a 0.00 correctness share
#: off exactly one of them — "No material change versus the prior read; …" and
#: "No material change – the pressure stays moderate as in the prior read …".
#:
#: THE RULE IS ANCHORED ON THE PRIOR READ, NEVER ON THE NEGATION. ANNEX C v4
#: rule 3 says absence claims ARE claims and names "no material change" among
#: its own examples; "no new disruptions", "no new incidents", "has not
#: deteriorated" are world claims the rubric was revised to grade and they must
#: stay gradeable. So every pattern below requires the continuity assertion to
#: be GRAMMATICALLY JOINED to a reference to the unit's own earlier read
#: ("change … since/versus/compared with the prior …", "unchanged from the
#: previous …", "as in the prior read"). A span that merely mentions a prior
#: read while asserting something about the world ("the high pressure
#: highlighted in the prior read persists, with continued fuel-price spikes")
#: keeps its claim and is graded. Measured over the 980 live claim rows the
#: joined form excludes 33 and leaves every world absence claim standing; the
#: looser "a prior-read mention anywhere plus a no-change phrase anywhere" form
#: excluded 49 and swallowed two genuine world claims.
PRIOR_RELATIVE_REASON = "prior_relative"

PRIOR_RELATIVE_DETAIL = (
    "the assertion is about this read's relation to its OWN previous read; the "
    "reference is a window of world developments and carries no prior read, so "
    "no label it could return would be a measurement"
)

#: "prior" / "previous" / "preceding" / "earlier" / "last" — the read this read
#: is comparing itself to. Bare, because the live corpus names it both with a
#: head noun ("the prior read", "the previous assessment") and without ("since
#: the previous window", "compared with the prior 18 September read").
_PRIOR_WORD = r"(?:prior|previous|preceding|earlier|last)"
#: The RELATION word. This is the load-bearing half: it is what turns a mention
#: of an earlier read into a comparison AGAINST it.
_PRIOR_RELATION = (
    r"(?:since|versus|vs\.?|from|compared\s+with|compared\s+to|"
    r"relative\s+to|against)"
)
#: The read's own name for itself.
_PRIOR_HEAD = r"(?:read|reads|assessment|assessments)"

PRIOR_RELATIVE_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # "No material change since the prior read" / "No material change in
    # capability … compared with the prior 18 September read".
    ("no_change_versus_prior", re.compile(
        r"\bno\s+(?:material|significant|substantive|notable|meaningful|"
        r"further|real|appreciable)?\s*change\b[^.;]{0,90}?\b"
        + _PRIOR_RELATION + r"\s+(?:the\s+)?" + _PRIOR_WORD + r"\b",
        re.IGNORECASE)),
    # "the pressure level is unchanged from the prior read", "persists
    # unchanged since the prior assessment".
    ("unchanged_from_prior", re.compile(
        r"\bunchanged\b[^.;]{0,40}?\b" + _PRIOR_RELATION
        + r"\s+(?:the\s+)?" + _PRIOR_WORD + r"\b", re.IGNORECASE)),
    # "the assessment remains moderate as in the prior read".
    ("as_in_the_prior_read", re.compile(
        r"\bas\s+in\s+the\s+" + _PRIOR_WORD + r"\s+(?:\w+\s+){0,2}?"
        + _PRIOR_HEAD + r"\b", re.IGNORECASE)),
    # "… were already noted in the previous assessment".
    ("already_noted_in_the_prior_read", re.compile(
        r"\b(?:already|previously)\s+(?:noted|recorded|reported|stated|"
        r"flagged|assessed|judged|found)\s+in\s+the\s+" + _PRIOR_WORD
        + r"\s+(?:\w+\s+){0,2}?" + _PRIOR_HEAD + r"\b", re.IGNORECASE)),
    # "the posture remains as previously assessed".
    ("remains_as_previously_assessed", re.compile(
        r"\bremains?\s+as\s+previously\s+(?:assessed|noted|recorded|"
        r"reported|stated|judged)\b", re.IGNORECASE)),
    # "no new development alters the outlook" — the prior read's own standing
    # judgement is the object, so the claim is again about this read and the
    # last one. The determiner must be THE/OUR/THIS and at most one adjective
    # may sit in front of the noun, which is what keeps "does not alter the
    # underlying military posture" (a world claim) out.
    ("nothing_alters_the_standing_read", re.compile(
        r"\b(?:no|nothing|none|neither)\b[^.;]{0,80}?\b(?:alters?|changes?|"
        r"shifts?|revises?|overturns?)\s+(?:the|our|this)\s+"
        r"(?:\w+\s+){0,1}?(?:outlook|assessment|picture|judgment|judgement|"
        r"read|call|view|conclusion|baseline)\b", re.IGNORECASE)),
)


def prior_relative_pattern(text: str) -> str | None:
    """Which prior-relative pattern this span matches, or ``None``.

    Returned by NAME rather than as a bool so the ledger records WHICH shape
    excluded the span — an exclusion nobody can audit is an exclusion nobody
    should trust, and that applies to this class exactly as it does to the
    machine-line rules above it.
    """
    body = (text or "").strip()
    if not body:
        return None
    for name, pattern in PRIOR_RELATIVE_RES:
        if pattern.search(body):
            return name
    return None


#: The redactor's notice, DERIVED from the constant rather than retyped — the
#: segmenter splits it into two spans and both are substrings of it. A retyped
#: copy would stop matching the day the notice was reworded, and the notice
#: would then be graded as a world claim.
_NOTICE_NORM = norm(REDACTION_NOTICE)


def base_exclusion_reason(text: str) -> str | None:
    """Step 1's registered rules (``build_p1_sample.exclusion_reason``).

    Order matters for the REPORT only: a text can be both short and machine,
    and the strict registered shape is the one worth naming when it applies.
    """
    body = (text or "").strip()
    if _MACHINE_STRICT_RE.search(body):
        return "machine_line_registered_shape"
    if body.count("·") >= 3 and _MACHINE_TELL_RE.search(body):
        return "machine_line_middot_fields_with_machine_tell"
    if len(body) < MIN_ASSERTION_CHARS:
        return f"under_{MIN_ASSERTION_CHARS}_chars"
    return None


def _is_heading(text: str) -> bool:
    body = (text or "").strip()
    if not body:
        return False
    if body.startswith("#"):
        return True
    return bool(re.fullmatch(r"\*\*[^*]{1,120}\*\*:?", body))


def _roster_reason(text: str, dimension_names: frozenset[str]) -> str | None:
    """A roster/enumeration line, or ``None``.

    ``dimension_names`` is READ from the shipped ``scorecard_banding.DIMENSIONS``
    by the caller, never typed in here.
    """
    body = (text or "").strip()
    if _ROSTER_PREFIX_RE.search(body):
        return "roster_enumeration_prefix"
    if _ROSTER_ALL_RE.search(body):
        return "roster_enumeration_all_verified_reads"
    bare, _ = strip_markers(body)
    tokens = [t for t in re.split(r"[^A-Za-z0-9_]+", bare.lower()) if t]
    if not tokens:
        return None
    dims = {d.lower() for d in dimension_names}
    # A dimension name survives tokenisation as its underscored whole
    # ("leadership_transition") or as its words ("leadership", "transition").
    dim_words = {w for d in dims for w in d.split("_")} | dims
    named = {t for t in tokens if t in dims}
    if len(named) < 2:
        return None
    if all(t in dim_words or t in _ROSTER_FILLER for t in tokens):
        return "roster_enumeration_dimension_list"
    return None


def exclusion_reason(text: str, dimension_names: frozenset[str]) -> str | None:
    """Why this span carries no gradable world claim, or ``None`` if it does.

    The order is the order the reasons are worth NAMING, not a precedence
    trick: a machine line is also short, and calling it short would bury what
    it is.
    """
    body = (text or "").strip()
    if not body:
        return "empty"
    normalised = norm(body)
    if normalised and normalised in _NOTICE_NORM:
        return "round_notice"
    if _is_heading(body):
        return "heading"
    why = base_exclusion_reason(body)
    if why:
        return why
    roster = _roster_reason(body, dimension_names)
    if roster:
        return roster
    for name, pattern in _BOOKKEEPING_RES:
        if pattern.search(body):
            return f"read_bookkeeping_not_world:{name}"
    # LAST, deliberately. Every rule above names a SHAPE (a machine line, a
    # heading, a roster, the assembly's own bookkeeping) and a shape is the
    # more informative thing to call a span. This one is about the ASSERTION,
    # so it only ever fires on a span nothing structural excluded.
    prior = prior_relative_pattern(body)
    if prior:
        return f"{PRIOR_RELATIVE_REASON}:{prior}"
    return None


# ---------------------------------------------------------------------------
# The segmentation
# ---------------------------------------------------------------------------


def segment_head(
    head: Mapping[str, Any],
    segmenter: Callable[[str], Sequence[str]],
    judge: Callable[[str], bool],
    dimension_names: frozenset[str],
) -> dict[str, Any]:
    """One head -> ``(atoms, excluded spans)``, each with its reason and position.

    EVERY span the shipped segmenter produced is accounted for: kept as an atom,
    or excluded with the rule that excluded it. The ledger IS the audit.
    """
    body = head.get("grader_body") or ""
    spans = list(segmenter(body))
    atoms: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for index, span in enumerate(spans):
        position = {"span_index": index, "char_offset": body.find(span)}
        if not judge(span):
            excluded.append({
                "span_index": index,
                "reason": "not_judgeable_span",
                "detail": "verify._is_judgeable_claim rejected it (heading, "
                          "as-of line, forward-looking, or fewer than two words)",
                "text": span,
            })
            continue
        why = exclusion_reason(span, dimension_names)
        if why:
            row = {"span_index": index, "reason": why, "text": span}
            if why.startswith(f"{PRIOR_RELATIVE_REASON}:"):
                row["detail"] = PRIOR_RELATIVE_DETAIL
            excluded.append(row)
            continue
        text, markers = strip_markers(span)
        # The 40-character floor is a rule about the CLAIM, so it is applied
        # again to what the grader will actually read: a span only long enough
        # because of its citation markers is not a 40-character claim.
        if len(text) < MIN_ASSERTION_CHARS:
            excluded.append({
                "span_index": index,
                "reason": f"under_{MIN_ASSERTION_CHARS}_chars_after_markers",
                "text": span,
            })
            continue
        position["atom_index"] = len(atoms)
        atoms.append({
            "id": atom_id(
                str(head["grain"]), str(head["analyst_id"]),
                str(head.get("created_at")), span,
            ),
            "grain": head["grain"],
            "analyst_id": head["analyst_id"],
            "output_id": head.get("output_id"),
            "created_at": head.get("created_at"),
            "text": text,
            "text_raw": span,
            "citation_markers": markers,
            "position": position,
        })
    return {
        "analyst_id": head["analyst_id"],
        "grain": head["grain"],
        "output_id": head.get("output_id"),
        "created_at": head.get("created_at"),
        "chars": len(body),
        "n_spans": len(spans),
        "n_kept": len(atoms),
        "n_excluded": len(excluded),
        "atoms": atoms,
        "excluded": excluded,
    }


def segment_all(
    heads: Sequence[Mapping[str, Any]],
    segmenter: Callable[[str], Sequence[str]],
    judge: Callable[[str], bool],
    dimension_names: frozenset[str],
) -> dict[str, Any]:
    """Every head's atoms and exclusions, with the totals a number rests on."""
    per_head = [
        segment_head(h, segmenter, judge, dimension_names) for h in heads
    ]
    atoms = [a for h in per_head for a in h["atoms"]]
    seen: dict[str, dict[str, Any]] = {}
    for atom in atoms:
        if atom["id"] in seen:
            raise SegmentationError(
                f"atom id collision {atom['id']} between "
                f"{seen[atom['id']]['analyst_id']} and {atom['analyst_id']} — "
                "two claims a grader could not tell apart. Refusing to ship."
            )
        seen[atom["id"]] = atom
    reasons: dict[str, int] = {}
    for head in per_head:
        for row in head["excluded"]:
            reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
    by_grain: dict[str, int] = {}
    by_analyst: dict[str, int] = {}
    for atom in atoms:
        by_grain[atom["grain"]] = by_grain.get(atom["grain"], 0) + 1
        by_analyst[atom["analyst_id"]] = by_analyst.get(atom["analyst_id"], 0) + 1
    return {
        "per_head": per_head,
        "claims": sorted(atoms, key=lambda a: a["id"]),
        "totals": {
            "n_spans": sum(h["n_spans"] for h in per_head),
            "n_kept": len(atoms),
            "n_excluded": sum(h["n_excluded"] for h in per_head),
            "excluded_by_reason": dict(sorted(reasons.items())),
            "kept_by_grain": dict(sorted(by_grain.items())),
            "kept_by_analyst": dict(sorted(by_analyst.items())),
        },
    }


__all__ = [
    "GRAIN_COMPOSITION",
    "GRAIN_DESK",
    "GRAIN_PREFIX",
    "MIN_ASSERTION_CHARS",
    "PRIOR_RELATIVE_DETAIL",
    "PRIOR_RELATIVE_REASON",
    "PRIOR_RELATIVE_RES",
    "REDACTION_NOTICE",
    "SegmentationError",
    "atom_id",
    "base_exclusion_reason",
    "exclusion_reason",
    "norm",
    "prior_relative_pattern",
    "redact_render",
    "segment_all",
    "segment_head",
    "strip_markers",
]
