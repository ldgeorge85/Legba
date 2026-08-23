# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assessability — the judge subsystem's second extracted brick (Q-1, R-train).

One question, asked twice: **did we actually check anything, and may we call the
result a score?**

The first half is CLAIM SHAPE — which segmented spans carry a proposition the
graders may grade. The second half is SCORE STATE — whether the tallies those
graders produced are entitled to be published as a faithfulness *number* at all.
They live together because the measured defect ran straight through both: a
labeled-scaffold rule swallowed whole finding bodies, the claim list came back
empty, and an empty claim list scored **1.0**. Fixing either half alone leaves
the other free to re-tell the same lie.

The measured class (2026-08-04 adjudication annex §9, re-measured 08-05 on the
live DB): 11 critiques in the trailing 7 days carried ZERO claim verdicts, all
11 scored ``overall_score = 1.0``, and 10 of them graded findings of 1,026-2,091
characters — full, substantive, heavily-cited analytic prose. The Italy energy
read asserted red heat-wave alerts across 25-27 cities, a magnitude-4.7 Campi
Flegrei earthquake with evacuations, and repeated Italian navy boardings of a
sanctioned shadow-fleet tanker. Every one of those is checkable. Every one was
cited. None was graded, and the finding was stamped perfect.

``verify`` imports these names one way; nothing here imports ``verify`` at load
time (the ``_verify()`` late-bind is the judge-subsystem convention, used only
where a shared verify-module name is genuinely needed).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:  # pragma: no cover — annotations only
    from uuid import UUID

    from .verify import FaithfulnessReport

# ---------------------------------------------------------------------------
# 1. CLAIM SHAPE — the labeled-scaffold rule, corrected
# ---------------------------------------------------------------------------

# #116(b): a BOLDED label:value line — ``**Severity:** High``, ``**Confidence:**
# Moderate``, ``**Time horizon:** 3-6 months`` — is document SCAFFOLDING the
# assessor stamps onto a finding, NOT a first-order citable fact. The bold run is
# the reliable signal (a plain ``Foo: bar`` sentence stays a claim); the colon
# may sit INSIDE the bold (``**Severity:**``) or right after it (``**Severity**:``).
# FLOOR-ONLY exemption — the judge still grades these spans (H1: exemptions must
# not hide a claim from the judge).
#
# MOVED here from verify.py 2026-08-05 (Q-1) together with the correction below.
_LABELED_SCAFFOLD_RE = re.compile(
    r"^(?:[-*>]\s+)*"                    # optional list / blockquote bullets
    r"\*\*[^*\n:]{1,48}"                # ** + a short label (no colon/star yet)
    r"(?::\s*\*\*|\*\*\s*:)",           # colon INSIDE the bold, or right AFTER it
    re.IGNORECASE,
)

#: Citation markers, stripped before a scaffold VALUE is measured — ``High [3]``
#: is a one-word value, not a two-token one.
_MARKER_STRIP_RE = re.compile(r"\[\[ref:[^\]]{1,64}\]\]|\[\d+(?:\s*-\s*\d+)?\]")

#: The longest a labeled span's remainder may be and still be a VALUE rather than
#: PROSE. ``**Severity:** High`` (1), ``**Time horizon:** 3-6 months`` (2),
#: ``**Confidence:** moderate, trending down`` (4) are stamps. The measured
#: failures ran 12-31 words: ``- **Heat-wave alerts:** Red alerts have been issued
#: for 25-27 of Italy's major cities, creating record electricity demand. [6][42]``
#: is a fully-formed factual assertion wearing a label, and the gap between the
#: two populations is wide enough that the exact cut is not load-bearing.
_SCAFFOLD_VALUE_MAX_WORDS = 6

#: …and a value that TERMINATES like a sentence is prose — but only once it is
#: long enough for the terminator to mean anything. ``**BLUF:** Tehran resumed
#: enrichment.`` is a complete assertion; ``**Large-scale exercise:** not
#: observed.`` is a two-word verdict stamp that happens to carry a full stop,
#: and the assessors punctuate stamps that way constantly. Requiring BOTH a
#: terminator and four words is what separates them; the terminator alone
#: reclassified every ``not observed.`` line as a claim and took the W31
#: unscoped-absence backstop off its own shapes.
_SENTENCE_END_RE = re.compile(r"[.!?][\"'”’)\]]*$")

#: Words a TERMINATED value needs before the terminator counts as a sentence.
_SCAFFOLD_TERMINATED_MIN_WORDS = 4


def labeled_scaffold_value(claim: str) -> str | None:
    """The VALUE a ``**Label:**`` line carries, or ``None`` when it is not one.

    Returns the post-label remainder with citation markers stripped. ``None``
    means the span does not have the labeled-scaffold SHAPE at all; ``""`` means
    it has the shape and carries nothing after the label (a bare stamp).
    """
    s = claim.strip()
    m = _LABELED_SCAFFOLD_RE.match(s)
    if not m:
        return None
    return _MARKER_STRIP_RE.sub(" ", s[m.end():]).strip()


#: Labels whose VALUE is by construction the analyst's derived read rather than a
#: first-order citable fact — the labeled spelling of the ``Assessment:`` /
#: ``Judgment:`` openers ``_SYNTHESIS_PREFIXES`` has always exempted from the
#: FLOOR. Without this the Q-1 fix would treat ``**Severity:** elevated — no
#: nationwide blackout has been reported`` as an uncited fact while treating
#: ``Assessed: elevated — no nationwide blackout has been reported`` as synthesis,
#: which is the same sentence twice. FLOOR-ONLY, exactly like its prefix twin: the
#: judge still grades these spans (H1), and a fabricated severity call dressed as a
#: stamp is still caught semantically.
_ASSESSMENT_LABELS = frozenset(
    {
        "assessment", "assessed", "judgment", "judgement", "bluf", "bottom line",
        "severity", "confidence", "trajectory", "near-term trajectory",
        "near term trajectory", "outlook", "time horizon", "horizon", "trend",
        "risk", "risk level", "so what", "implication", "implications",
        "significance", "verdict", "call",
    }
)


#: The core plane emits typographic dashes inside labels (``Near‑term`` carries a
#: U+2011 NON-BREAKING HYPHEN, not an ASCII ``-``). Matching a label set on the raw
#: bytes silently misses those — the same class of defect as the full-width 【N】
#: citation brackets — so normalize before the lookup.
_DASH_CHARS = "‐‑‒–—―−"
_DASH_TRANS = str.maketrans({c: "-" for c in _DASH_CHARS})


def labeled_scaffold_label(claim: str) -> str | None:
    """The LABEL of a ``**Label:**`` line, normalized + lowercased, or ``None``."""
    s = claim.strip()
    m = _LABELED_SCAFFOLD_RE.match(s)
    if not m:
        return None
    label = m.group(0).strip().lstrip("-*> ").strip("*: ").strip().lower()
    return " ".join(label.translate(_DASH_TRANS).split())


def is_assessment_scaffold(claim: str) -> bool:
    """Is this a labeled span whose label marks it as a DERIVED READ?

    Floor-exempt (synthesis), judge-graded. See ``_ASSESSMENT_LABELS``.
    """
    label = labeled_scaffold_label(claim)
    return label is not None and label in _ASSESSMENT_LABELS


def is_labeled_scaffold(claim: str) -> bool:
    """True only for a span that is REALLY scaffolding — a short label:value stamp.

    THE Q-1 CORRECTION. The old test was ``_LABELED_SCAFFOLD_RE.match(...)`` and
    nothing else, so the exemption keyed on the LABEL and never once looked at
    what followed it. That is fine for the shape it was written for
    (``**Severity:** High``) and catastrophic for the shape the assessors
    actually emit most: a bullet whose bold run is a SIGNPOST introducing a
    paragraph of cited fact. Both hit the same regex; only one is scaffolding.

    So the rule now reads the remainder. A short value stays exempt exactly as
    before. A remainder carrying real prose is NOT scaffolding — the label is a
    signpost, the prose is a claim, and the claim goes to the graders. Callers
    that previously wrote ``_LABELED_SCAFFOLD_RE.match(s)`` call this instead;
    the position of the test in their rule ladder is UNCHANGED, so a
    ``**BLUF:** …`` line still lands on the synthesis exemption below it and a
    ``**Severity:** High`` stamp still lands here.
    """
    value = labeled_scaffold_value(claim)
    if value is None:
        return False
    words = len(value.split())
    if _SENTENCE_END_RE.search(value) and words >= _SCAFFOLD_TERMINATED_MIN_WORDS:
        return False
    return words <= _SCAFFOLD_VALUE_MAX_WORDS


# ---------------------------------------------------------------------------
# 2. THE JSON TRIPWIRE
# ---------------------------------------------------------------------------

#: A span that OPENS as a JSON key line (``"verdict": "supported",``), optionally
#: behind the punctuation a sentence splitter leaves on the front of one.
_JSON_KEY_LINE_RE = re.compile(r'^[\s\{\[\],]*"[^"\n]{1,120}"\s*:\s*')

#: A span that is bare JSON/structural punctuation and nothing else.
_JSON_PUNCT_ONLY_RE = re.compile(r'^[\s\{\}\[\],:"\']+$')


def is_json_syntax_claim(claim: str) -> bool:
    """Is this span literal JSON syntax that leaked into a finding body?

    Q-1(d), the tripwire. ``cross_doc_corroborator`` has been shipping raw tool
    preamble and UNPARSED JSON into the substrate as a finding body (adjudication
    §6.4). The segmenter has no opinion about JSON, so ``"verdict": "supported",``
    reaches the ledger as a graded *claim* — a machine artefact scored as though
    it were an analyst's assertion, in both directions (a fabricated support and
    a fabricated miss are equally available).

    A dropped span is COUNTED, never graded — the counter is what makes a
    producer's broken output contract visible from the critique row alone
    instead of requiring someone to read bodies.
    """
    s = claim.strip()
    if not s:
        return False
    # A CITATION-marker span is not JSON, however much ``[1]`` looks like a JSON
    # array to a parser. Both classes end up dropped either way, so this changes
    # no score — but it changes which COUNTER fires, and the JSON counter's whole
    # job is to point at a producer with a broken output contract. Firing it on an
    # ordinary orphaned marker would send someone hunting a defect that isn't
    # there, which is worse than not counting at all.
    if not _MARKER_STRIP_RE.sub(" ", s).strip(" \t.,;:"):
        return False
    if _JSON_PUNCT_ONLY_RE.match(s):
        return True
    if _JSON_KEY_LINE_RE.match(s):
        return True
    # A span that PARSES as a JSON object/array is JSON whatever it looks like.
    # Scalars are excluded on purpose: ``"Tehran resumed enrichment"`` is valid
    # JSON and is also a claim.
    if s[0] in "{[":
        try:
            return isinstance(json.loads(s), (dict, list))
        except (ValueError, TypeError):
            return False
    return False


# ---------------------------------------------------------------------------
# 2b. V-I6 (2026-08-05) — WHAT IS IN THE DENOMINATOR. Two counters, no gate.
#
# The round-4 panel passed the pass-side gate (0 misses at n=10) and attached two
# caveats to the pass, both about the SUPPORTED DENOMINATOR rather than about any
# individual verdict:
#
#   1. **The `triggered indicator:` pseudo-claims.** 97 such ledger entries in
#      the frozen population, ALL supported, inflating the supported denominator
#      by ~4.6%. The class is not new — 194 entries under `2026-08-02/1`, 230
#      under `2026-08-03/1` — and the panel kept it in the sampling frame for
#      method continuity while naming it plainly: "a scaffold artifact, not a
#      proposition". Its P2 specimen: *"triggered indicator: Electoral loss or
#      intra-elite disagreement signaling ruling coalition strain."*
#   2. **Coverage statements counted as substantive.** P4: *"All five country
#      reads (United States, Canada, Brazil, Argentina, Mexico) were included; no
#      target lacked a read."* True, verified, and a statement about the
#      platform's own input set rather than about the world.
#
# COUNTERS, NOT GATES, and deliberately so. Both classes are currently INSIDE the
# arithmetic; removing them would move every pass-side score in a train that also
# carries five severity changes, and nobody would then be able to say which
# change moved the number. So the arithmetic is untouched and the size of each
# class becomes readable off the critique row — which is the precondition for
# deciding the drop, not a substitute for it. Recorded as owed.
# ---------------------------------------------------------------------------

#: The `triggered indicator:` scaffold rows, counted (never dropped here).
DENOMINATOR_TRIGGERED_INDICATOR = "denominator_triggered_indicator_scaffold"
#: Claims about the platform's OWN coverage / input set rather than the world.
DENOMINATOR_COVERAGE_STATEMENT = "denominator_coverage_statement"

#: The platform's own coverage vocabulary. Bounded and specific: "country reads",
#: "unit reads", "no target lacked a read" are things only this system says about
#: itself, and an ordinary sentence about reading is not swept up.
_COVERAGE_STATEMENT_RE = re.compile(
    r"\b(?:"
    r"(?:country|unit|desk|regional|member|analyst|sub)\s+reads?\b"
    r"|reads?\s+(?:were|was)\s+(?:available|included|produced|missing)"
    r"|(?:no\s+target|none)\s+lacked\s+a\s+read"
    r"|lacked\s+a\s+read\b"
    r"|composed\s+from\s+\d+\s+\w+\s+reads?"
    r"|produced\s+(?:a\s+)?reads?\s+this\s+cycle"
    r"|coverage\s*[:\-—–]"
    r")",
    re.IGNORECASE,
)


def is_coverage_statement(claim: Any) -> bool:
    """Is this claim about the platform's OWN coverage rather than the world?"""
    return isinstance(claim, str) and bool(_COVERAGE_STATEMENT_RE.search(claim))


def denominator_caveat_counters(*, body: str, indicators: Any) -> dict[str, int]:
    """The two round-4 pass-side caveats, as counts. Never raises, never gates.

    Both are measured off the SAME inputs the graders saw, so a panel can
    reconcile them against the ledger without re-deriving anything from claim
    text — the V-G8 fidelity rule applied to the pass side.
    """
    out: dict[str, int] = {}
    triggered = 0
    if isinstance(indicators, (list, tuple)):
        triggered = sum(
            1
            for e in indicators
            if isinstance(e, Mapping) and e.get("status") == "triggered"
        )
    if triggered:
        out[DENOMINATOR_TRIGGERED_INDICATOR] = triggered
    coverage = sum(
        1
        for claim in _verify()._segment_claims(body or "")
        if _verify()._is_judgeable_claim(claim) and is_coverage_statement(claim)
    )
    if coverage:
        out[DENOMINATOR_COVERAGE_STATEMENT] = coverage
    return out


# ---------------------------------------------------------------------------
# 3. SCORE STATE — what a tally is entitled to be called
# ---------------------------------------------------------------------------

#: The report carries a real measurement over a real claim list.
SCORE_STATE_SCORED = "scored"
#: The report carries NO measurement. Not 1.0, not 0.0 — a non-score.
SCORE_STATE_UNASSESSABLE = "unassessable"

#: A body at or above this many characters is SUBSTANTIVE — long enough that
#: extracting nothing (or almost nothing) from it is evidence about the extractor,
#: not about the finding. The measured zero-claim bodies ran 1,026-2,091 chars.
SUBSTANTIVE_BODY_CHARS = 800

#: Below this many checkable claims, a SUBSTANTIVE body has not been meaningfully
#: assessed. One claim pulled out of 1,500 characters of cited analysis is the
#: same defect as zero, one notch quieter.
MIN_ASSESSABLE_CLAIMS = 2

#: The gate score an UNASSESSABLE report publishes. It is deliberately neither a
#: pass nor a crushing: the finding did nothing wrong, our pass did. Set at the
#: verify floor so ``effective_confidence = min(confidence, overall_score)``
#: lands the finding exactly ON the floor — visible, uncertified, not buried.
UNASSESSABLE_GATE_SCORE = 0.5

#: The ceiling a PROVISIONAL (non-``llm``) verdict may publish. The deterministic
#: floor can detect a missing citation; it structurally cannot confirm that a
#: cited claim is faithful to what it cites — that is the judge's entire job. So
#: a floor-only run may report a healthy score and may never report a CERTIFIED
#: one. 0.85 sits below the band a reader treats as adjudicated-clean while
#: leaving the floor's ordinary range untouched.
PROVISIONAL_SCORE_CEILING = 0.85


def resolve_score_state(
    *, checkable_claims: int, body: str
) -> tuple[str, str | None]:
    """``(score_state, reason)`` for a completed tally.

    THE Q-1 CORRECTION, second half. ``score = 1.0 if checkable == 0 else …``
    was written as "we never invent a defect", which is right, and it published
    the answer as a *faithfulness score*, which is wrong. 1.0 there means "we
    found no claim", and every downstream consumer — confidence gating, alert
    routing, calibration, gold-set construction — reads it as "every claim
    checked out". Those two sentences are opposites and the schema could not
    tell them apart.

    So an empty (or near-empty) check now reports its own state instead of
    borrowing the top of the scale. Nothing here decides a NUMBER; the caller
    keeps its arithmetic and asks this what the arithmetic is allowed to be
    called.
    """
    if checkable_claims <= 0:
        return SCORE_STATE_UNASSESSABLE, "no_checkable_claims"
    if (
        checkable_claims < MIN_ASSESSABLE_CLAIMS
        and len(body or "") >= SUBSTANTIVE_BODY_CHARS
    ):
        return SCORE_STATE_UNASSESSABLE, "thin_claims_on_substantive_body"
    return SCORE_STATE_SCORED, None


def gate_score(
    *, score: float, ceiling: float | None, score_state: str, provisional: bool
) -> float:
    """The single number the finding↔critique gate folds into confidence.

    Three caps, applied in order and all of them downward:

    * the T7 evidence ceiling (a composition may not exceed its strongest
      independent cited sub-claim) — unchanged;
    * ``UNASSESSABLE_GATE_SCORE`` when there is no measurement to publish;
    * ``PROVISIONAL_SCORE_CEILING`` when the judge did not grade.

    Never raises a score. A report that is both unassessable and provisional
    takes the lower of the two, which is the unassessable floor.
    """
    out = score if ceiling is None else min(score, ceiling)
    if score_state == SCORE_STATE_UNASSESSABLE:
        out = min(out, UNASSESSABLE_GATE_SCORE)
    if provisional:
        out = min(out, PROVISIONAL_SCORE_CEILING)
    return out


def is_provisional(judge_status: Any) -> bool:
    """A verdict the LLM judge did not produce is PROVISIONAL.

    Covers every non-``llm`` path with one test: the flag being off, the judge
    erroring (``judge_error`` — the 26-hour outage that produced 611 scored
    critiques and a 0.21 fleet-wide mean drop with no alarm), a degraded
    response, or a caller that wired no judge at all. The distinction the
    surfaces owe their reader is not WHICH of those happened — the existing
    ``judge_unavailable_reason`` already says that — it is that the number in
    front of them was never adjudicated.
    """
    return str(judge_status or "") != "llm"


# ---------------------------------------------------------------------------
# 3b. JUDGE SAMPLING — which findings the LLM judge grades at all
# ---------------------------------------------------------------------------
# J2 (2026-08-15, FORWARD_PLAN §1). The judge plane moved to a rate-limited
# free tier (~50 calls/day), so verification is SAMPLED, not exhaustive. The
# policy below decides, per finding, whether the LLM judge runs. Three rules,
# all of them replayable:
#
#   * DETERMINISTIC, NO RNG. The sample coordinate is a hash of the finding id
#     mapped into [0, 1); a finding is judged iff its coordinate < the rate.
#     The same finding id yields the same verdict on every replay, every
#     harness, every host — a random.random() here would make the judged
#     population unreconstructable.
#   * THE ALWAYS-LIST WINS. Kinds (or analyst ids) named in
#     ``judge_sample_always`` are judged regardless of the rate. The default
#     names the synthesis tops — compositions + world + journal — because a
#     fabricated composition under the system's seal costs more than an
#     unsampled unit finding, and their combined volume (~25/day) fits the
#     budget.
#   * AN ABSENT RATE IS NO GATE. ``rate=None`` (no descriptor option) judges
#     everything, byte-identical to the pre-J2 tree. The gate only exists
#     where a descriptor says so.
#
# An UNSAMPLED finding keeps the deterministic floor + the PROVISIONAL ceiling
# exactly as a judge-off run does, but publishes ``judge_status='unsampled'``
# — a new HONEST state. Never 'error': nothing failed, and never
# 'deterministic'-with-a-reason: the row was not skipped by an outage, it was
# deliberately not selected. ``overall_score`` stays a real float (the SQL
# laterals filter on it), so unsampled rows demote-and-remain-visible instead
# of vanishing from the goldset/archiver/composition basis.

#: ``FaithfulnessReport.judge_status`` for a finding the sampling gate
#: deliberately did not send to the LLM judge. Non-``llm`` ⇒ provisional
#: (``is_provisional`` needs no change), so the 0.85 ceiling applies.
JUDGE_STATUS_UNSAMPLED = "unsampled"

#: The default ``judge_sample_always`` membership: the finding KINDS that are
#: always judged when a rate is set and no explicit list overrides it.
#: Compositions (country/region/escalation/world ride meta_findings_synthesizer;
#: the correlator and the situation tracker grade through the same composition
#: verify path) + the journal family. Unit findings (``inline_target``) are the
#: sampled population.
JUDGE_SAMPLE_ALWAYS_DEFAULT: tuple[str, ...] = (
    "meta_findings_synthesizer",
    "cross_analyst_correlator",
    "situation_tracker",
    "journal_assessor",
)


def judge_sample_unit(finding_id: Any) -> float:
    """The finding's deterministic sample coordinate in ``[0, 1)``.

    SHA-256 over the canonicalized id string (stripped, lowercased — so a UUID
    object, its str, and a case-varied hex spelling all land identically),
    first 8 bytes as an unsigned int over 2^64. Uniform, stable across
    processes/hosts/replays, and independent of everything except the id.
    """
    canonical = str(finding_id).strip().lower()
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2.0**64


@dataclass(frozen=True)
class JudgeSamplingPolicy:
    """The resolved per-finding sampling decision inputs (J2).

    Built by the CALLER (the actor seam, from descriptor options merged into
    the run options) and handed to ``verify_finding_faithfulness``; the
    decision itself is computed here so the verify path owns its own gate.
    ``rate=None`` means NO gate (judge everything — the pre-J2 contract);
    ``always=None`` means the code default membership above, while an explicit
    empty tuple CLEARS it (an operator saying "sample even the compositions").
    """

    finding_id: str
    kind: str = ""
    analyst_id: str = ""
    rate: float | None = None
    always: tuple[str, ...] | None = None

    def should_judge(self) -> bool:
        """``True`` ⇒ the LLM judge grades this finding (subject to the
        existing flag/handler gates); ``False`` ⇒ ``judge_status='unsampled'``.
        """
        if self.rate is None:
            return True
        always = (
            JUDGE_SAMPLE_ALWAYS_DEFAULT if self.always is None else self.always
        )
        if (self.kind and self.kind in always) or (
            self.analyst_id and self.analyst_id in always
        ):
            return True
        rate = float(self.rate)
        if rate <= 0.0:
            return False
        if rate >= 1.0:
            return True
        return judge_sample_unit(self.finding_id) < rate


# ---------------------------------------------------------------------------
# 4. THE CRITIQUE CONTRACT — where a tally becomes a published verdict
# ---------------------------------------------------------------------------
# Moved here from verify.py 2026-08-05 (Q-1). The payload builder is the single
# place a FaithfulnessReport turns into the row every downstream gate, alert,
# scorecard and badge reads, so the rule about what a tally may be CALLED (§3)
# and the code that calls it belong in one unit. Shared verify-module names
# late-bind through ``_verify()`` at call time — no load-time cycle.


def _verify():
    from . import verify
    return verify


def build_faithfulness_critique_payload(
    report: "FaithfulnessReport",
    *,
    analyzed_output_id: "UUID",
    analyzed_analyst_id: str = "",
    analyzed_analyst_version: str = "",
    analyzed_model: str = "",
    judge_model: str = "",
    judge_llm_ref: str = "",
    judge_route: str = "",
) -> dict[str, Any]:
    """Build the ``CritiquePayload``-shaped dict for the faithfulness verdict.

    Returns a plain dict (the runtime validates it against ``CritiquePayload``
    on write).  ``overall_score = faithfulness_score`` so the existing finding↔
    critique gate folds it into ``effective_confidence``; the unsupported spans
    + judge status live in the payload's ``data`` so the findings API can
    surface a ``verification`` block naming WHY confidence was demoted.

    The shape is built here (not in the kind module) so the verify seam owns its
    own critique contract; ``analyzed_output_id`` is the finding's id and the
    critic is the verify pass itself (the caller stamps the analyst_ctx).

    ``overall_score`` (the gate JOIN key) folds the T7 evidence ceiling:
    ``overall_score = min(faithfulness_score, confidence_ceiling)`` when the
    composition supplied a ceiling, else ``= faithfulness_score`` (the unit path
    is byte-identical — ``confidence_ceiling`` is ``None`` there). So the gate's
    ``effective_confidence = min(confidence, overall_score)`` yields
    ``min(confidence, faithfulness, sub-claim ceiling)`` for a composition — a
    hedge-laundering clause over a weak sub-claim demotes to ≤ that sub-claim,
    and correlated sub-claims cannot inflate the ceiling.

    P2-4 (additive, labels + persistence only — scores/floors/gates untouched):

      * ``judge_llm_ref`` — the RESOLVED judge stack-ref (the P2-4 JudgeRoute
        component id) stamped top-level (CritiquePayload field) AND into
        ``data.verification`` so provenance records which model judged, forever.
        ``""`` = floor-only (no judge wired).
      * ``judge_route`` (W-3d, additive) — the judge-route CLASS the ladder
        resolved: ``configured`` (env override / ``method.llm.judge``) |
        ``fallback_verify`` (``method.llm.verify`` — today's live rung) |
        ``fallback_primary`` (terminal rung). Stamped top-level AND into
        ``data.verification`` (which the findings API projects wholesale) so
        the UI provenance badge can tell an explicitly-configured judge from a
        ladder fallback. ``""`` = floor-only / pre-W-3d rows (the block then
        carries ``None``, never a fabricated class).
      * ``data.verification.claim_verdicts`` — the size-bounded per-claim
        verdict LEDGER (supported + hard_fail + soft_fail rows) with an honest
        ``claim_verdicts_truncated`` flag; each ``unsupported_spans`` entry
        additionally carries its ``fail_class`` (via ``UnsupportedSpan.as_dict``).
    """
    score = report.faithfulness_score
    ceiling = report.confidence_ceiling
    # The gate score — capped by the double-count-corrected evidence ceiling, and
    # (Q-1 b/c) by the unassessable floor and the provisional ceiling. Still a
    # REAL float, never NULL: thirteen SQL laterals filter
    # ``cr.data->>'overall_score' IS NOT NULL``, so writing a null here would not
    # publish "we could not check this" — it would make the finding vanish from
    # the goldset, the archiver, the situation tracker and the composition basis,
    # and re-surface it in the periphery tier labelled ``unverified``. That is a
    # different and worse lie than the one being fixed. The cap DEMOTES; the
    # ``score_state`` below is what SAYS SO.
    #
    # ``faithfulness_score`` is the OTHER half of round-4 rec #8, shipped
    # 2026-08-09 after round 5 measured the gap (§9b): both unassessable rows
    # still carried 1.0 there, so the raw tally entered the population mean and
    # every consumer reading ``faithfulness_score`` without ``score_state`` saw a
    # perfect score for producing nothing checkable. An unassessable report now
    # publishes NULL on that key — there is no measurement to publish — while
    # ``overall_score`` stays the real, capped float the laterals key on. The
    # panel's narrowed wording: "score null on score_state='unassessable'".
    overall = gate_score(
        score=score,
        ceiling=ceiling,
        score_state=report.score_state,
        provisional=report.provisional,
    )
    unassessable = report.score_state == SCORE_STATE_UNASSESSABLE
    # The published headline. An unassessable report has no score to headline, so
    # it says that instead of borrowing a number.
    score_label = "unassessable" if unassessable else f"{score:.2f}"
    # (#3) Advisory spans (double_counted / hedge_laundering) are structural
    # observations, NOT unsupported claims — exclude them so the tally reconciles
    # (supported + unsupported ≈ checkable) instead of over-counting.
    n_unsupported = sum(
        1 for s in report.unsupported_spans if s.reason not in _verify()._ADVISORY_REASONS
    )
    n_advisory = len(report.unsupported_spans) - n_unsupported
    judge_label = (
        report.judge_status
        if report.judge_unavailable_reason is None
        else f"judge-unavailable:{report.judge_unavailable_reason}"
    )
    body_lines = [
        f"Faithfulness verify of finding {analyzed_output_id}",
        f"  faithfulness_score={score_label}",
        f"  checkable_claims={report.checkable_claims} "
        f"supported={report.supported_claims} unsupported={n_unsupported}"
        + (f" advisory={n_advisory}" if n_advisory else ""),
        f"  judge={judge_label}",
    ]
    if unassessable:
        body_lines.append(
            f"  UNASSESSABLE ({report.score_state_reason}) — this pass extracted "
            f"no gradeable claim from the finding, so it has NOT been checked. "
            f"The gate score is capped at {overall:.2f}; it is not a verdict on "
            f"the finding's quality, it is the absence of one."
        )
    if report.provisional:
        body_lines.append(
            f"  PROVISIONAL — the LLM judge did not grade this finding, so the "
            f"deterministic floor is the whole verdict. A floor can find a "
            f"missing citation; it cannot confirm a cited claim is faithful. "
            f"Score ceiling {PROVISIONAL_SCORE_CEILING:.2f}."
        )
    if ceiling is not None:
        body_lines.append(
            f"  confidence_ceiling={ceiling:.2f} (double-count-corrected) "
            f"→ overall_score={overall:.2f}"
        )
    for span in report.unsupported_spans[:20]:
        body_lines.append(f"  - [{span.reason}] {span.text[:200]}")
    # V3 (MP:DEC-E) — per-branch telemetry (design §2.3 / §2.2). ``branch_scores``
    # is the pooled sub-score per JUDGED claim-kind (empty on the deterministic
    # path AND on the M14 whole-finding survey path, so those runs + a pre-V3
    # reader are byte-identical). ``branch_versions`` stamps the profile VERSION
    # of each kind that ran, so a recalibration is a visible, greppable, per-kind
    # version bump. Both are ADDITIVE JSONB keys ignored by existing readers (the
    # gate reads only ``overall_score``, which is unchanged).
    branch_scores = report.branch_scores or {}
    branch_versions = {
        kind: _verify()._JUDGE_PROFILES[kind].version
        for kind in branch_scores
        if kind in _verify()._JUDGE_PROFILES
    }
    # P2-4: the size-bounded per-claim ledger + honest truncation flag.
    claim_verdicts, claim_verdicts_truncated = _verify()._bounded_claim_verdicts(
        report.claim_verdicts
    )
    # The title PREFIX is load-bearing — every verify lateral in the system pins
    # ``cr.title LIKE 'Faithfulness verify%'``. Only the parenthetical changes.
    return {
        "title": (
            "Faithfulness verify (unassessable)"
            if unassessable
            else f"Faithfulness verify (score {overall:.2f})"
        ),
        "body": "\n".join(body_lines)[:65536],
        # Low surfaced confidence on the critique ROW itself when faithfulness is
        # poor, so the verify product reads as what it is. The GATE uses
        # overall_score (below), not this — but keep them coherent.
        "confidence": overall,
        "tags": (
            ["verify", "faithfulness", judge_label]
            + (["unassessable"] if unassessable else [])
            + (["provisional"] if report.provisional else [])
        ),
        "analyzed_output_id": analyzed_output_id,
        "analyzed_analyst_id": analyzed_analyst_id[:256],
        "analyzed_analyst_version": analyzed_analyst_version[:64],
        "analyzed_model": analyzed_model[:128],
        "judge_model": judge_model[:128],
        # P2-4: the RESOLVED judge stack-ref (JudgeRoute component id) —
        # provenance for which model judged, stamped on the row forever.
        "judge_llm_ref": judge_llm_ref[:256],
        # W-3d: the judge-route CLASS (configured|fallback_verify|
        # fallback_primary) — the badge's configured-vs-fell-back signal.
        "judge_route": judge_route[:32],
        # ``CritiquePayload.scores`` is typed ``dict[str, float]`` — an
        # unassessable report OMITS the key rather than carrying a null it
        # cannot: an absent score is the same statement.
        "scores": {} if unassessable else {"faithfulness": score},
        # The gate reads data->>'overall_score' off the analyst_outputs row; the
        # whole CritiquePayload is model_dumped into the data JSONB, so this
        # top-level field lands at data->>'overall_score' (the JOIN key).
        "overall_score": overall,
        # The verification detail the findings API surfaces (it reads
        # data->'data'->'verification').
        "data": {
            "verification": {
                # NULL when unassessable (rec #8, second half — see the
                # ``overall`` comment above); the raw tally otherwise.
                "faithfulness_score": None if unassessable else round(score, 4),
                "confidence_ceiling": (
                    round(ceiling, 4) if ceiling is not None else None
                ),
                "overall_score": round(overall, 4),
                "checkable_claims": report.checkable_claims,
                "supported_claims": report.supported_claims,
                "unsupported_spans": [s.as_dict() for s in report.unsupported_spans],
                "judge_status": report.judge_status,
                "judge_unavailable_reason": report.judge_unavailable_reason,
                # V3 per-branch telemetry (additive; {} when the judge did not run
                # or the M14 whole-finding survey rubric graded the entire list).
                "branch_scores": branch_scores,
                "branch_versions": branch_versions,
                # P2-4 additive fields: the judge-route provenance stamp, and the
                # full per-claim verdict ledger (supported verdicts included —
                # previously recorded nowhere), size-bounded with an honest flag.
                "judge_llm_ref": judge_llm_ref[:256] or None,
                # W-3d: the route CLASS behind the ref — the UI reads this
                # block wholesale, so the badge gets it with no route change.
                "judge_route": judge_route[:32] or None,
                "claim_verdicts": claim_verdicts,
                "claim_verdicts_truncated": claim_verdicts_truncated,
                # 2026-07-31 structural-fix RECEIPTS: the sparse counter map
                # (a counter appears only when it fired). Additive JSONB the
                # findings API projects wholesale — every fix in the train is
                # measurable from the critique row alone, with no log scrape.
                "counters": dict(report.counters),
                # Q-1 additive honesty keys. ``faithfulness_score`` above is the
                # RAW tally on a scored row and NULL on an unassessable one
                # (rec #8's second half — a tally over zero claims is not a
                # measurement); ``overall_score`` stays the published, capped
                # gate number — the two were always allowed to differ (T7
                # ceiling) and now differ for two more reasons. These three keys
                # are what let a reader tell "checked and clean" from "never
                # checked" from "checked by a floor that cannot certify" — three
                # states the schema previously rendered as one number.
                "score_state": report.score_state,
                "score_state_reason": report.score_state_reason,
                "provisional": report.provisional,
                "provisional_score_ceiling": (
                    PROVISIONAL_SCORE_CEILING if report.provisional else None
                ),
                # The population SPLIT key (see JUDGE_PIPELINE_VERSION): every
                # consumer of faithfulness history partitions on this, so
                # critiques graded under different verify pipelines are never
                # pooled and the train's upward shift reads as the MEASUREMENT
                # CORRECTION it is, not as a quality movement.
                "judge_pipeline_version": _verify().JUDGE_PIPELINE_VERSION,
            }
        },
    }


__all__ = [
    "DENOMINATOR_COVERAGE_STATEMENT",
    "DENOMINATOR_TRIGGERED_INDICATOR",
    "JUDGE_SAMPLE_ALWAYS_DEFAULT",
    "JUDGE_STATUS_UNSAMPLED",
    "JudgeSamplingPolicy",
    "MIN_ASSESSABLE_CLAIMS",
    "build_faithfulness_critique_payload",
    "denominator_caveat_counters",
    "judge_sample_unit",
    "PROVISIONAL_SCORE_CEILING",
    "SCORE_STATE_SCORED",
    "SCORE_STATE_UNASSESSABLE",
    "SUBSTANTIVE_BODY_CHARS",
    "UNASSESSABLE_GATE_SCORE",
    "_LABELED_SCAFFOLD_RE",
    "gate_score",
    "is_assessment_scaffold",
    "is_coverage_statement",
    "is_json_syntax_claim",
    "is_labeled_scaffold",
    "is_provisional",
    "labeled_scaffold_label",
    "labeled_scaffold_value",
    "resolve_score_state",
]
