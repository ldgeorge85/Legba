# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W-1 — THE CLAIM SOURCE, and the deterministic UNCHECKABLE pre-filter.

THE DEMOTION DID THE HARD HALF. Before ``assembly.v1`` the claim population of
a read was something a model had to *extract*: one core-plane call per head,
producing a list nobody could replay a month later. Under the assembly the read
IS a list of verbatim quoted spans with byte-exact origins
(``data.data.assembly.blocks[].spans[]``, each carrying
``origin.{head_id,start,end,body_sha256}``), so the population is a FIELD in the
payload. Zero LLM calls to enumerate it, and — the property a disputed
CONTRADICTED verdict actually needs — the same claim resolves to the same
:func:`claim_key` on replay a month later.

That is also why grading the assembly grades the **desks**. The assembly authors
nothing; its construction error is 1.0 by arithmetic (D-1 §5.3 G2). Anyone
reading the ``assembly_span`` number as "the composition tier's accuracy" is
reading it wrong, and every surface that publishes it says so.

THREE POPULATIONS, NEVER POOLED (design §0.4):

  * :data:`POPULATION_ASSEMBLY_SPAN` — the RECORD. One claim per
    ``blocks[].spans[]`` entry. The claim is a desk's own sentence, quoted.
  * :data:`POPULATION_ASSESSMENT_SENTENCE` — the VOICE. One claim per sentence
    of the Assessment channel's prose, which is the one composition-tier surface
    still making claims of its own after the demotion.
  * :data:`POPULATION_LEGACY_PROSE` — the flag-off / pre-flip arm, whose claims
    still come off the LLM extraction leg in ``standing_auditor``. This module
    does not produce those; it names the population so the ledger's three arms
    are declared in one place.

THE PRE-FILTER IS THE MOST IMPORTANT ARITHMETIC HERE, because it is what
separates a TRUTH number from a SEARCH-HEALTH number. ~9% of live claims have no
world truth-maker at all: they are about this system's own coverage, its own
slice, or its own packet. A loop that searches for one of those gets NOT_FOUND
and quietly deflates the headline. The class is therefore decided BEFORE the
query is issued, deterministically, and reported as its own count:

  * ``scope_bounded``  — the claim is bounded to this desk's collection/slice
    ("in this slice", "the collection", "as of <date>"). Detected off the span's
    own ``scope_tokens`` (which ``composition_integrity.has_collection_
    denominator_scope`` already wrote at assembly time) OR the lexicon below.
  * ``provenance``     — the truth-maker is the PACKET, not the world. R3's one
    non-resolving span in 282 (ML_A ``CR-2bbcd291``, *"a self-referential
    provenance claim whose truth-maker is the packet"*) and R2-C4 case #4
    (*"All eight principal units produced verified reads in this cycle."*).
  * ``perspective``    — the Assessment's own weighing. D-6 §A2 splits the
    voice's sentences into FACT (states what a block says, carries an ordinal)
    and PERSPECTIVE (an argument about the record). ~68% of Assessment sentences
    are perspective and none of them is a claim about the world.

A NOTE ON THE PERSPECTIVE SPLITTER, because honesty about what exists matters
more than a tidy sentence. D-6's FACT/PERSPECTIVE split is a *writing rule in a
prompt* (``assessment_prompts`` §A2) — there is no code splitter to import. So
:func:`perspective_shaped` is a deterministic DETECTOR over the same two
classes, built from the two signals the payload actually carries: a sentence
that names no spine ordinal is not stating what a block says, and a sentence
carrying a judgement/modal marker is an argument. It is calibrated against the
design's measured ~68% share, and it is a detector, not the prompt's rule.

WHAT THIS MODULE IS NOT. It never searches, never calls a model, never writes.
It is pure over a row's payload, which is what lets the whole claim population
be replayed from the date alone.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Sequence

# ``assembly_spans`` is a LEAF (hashlib, re, text_fold) — importing the role
# vocabulary from it keeps ONE definition of what a context span is, and
# adds no edge to this module's import graph beyond the one already here.
from ..assembly_spans import CONTEXT_SPAN_ROLES
from ...provenance.text_fold import normalize_for_match

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The three populations (design §0.4) — never pooled into one headline
# ---------------------------------------------------------------------------

POPULATION_ASSEMBLY_SPAN = "assembly_span"
POPULATION_ASSESSMENT_SENTENCE = "assessment_sentence"
POPULATION_LEGACY_PROSE = "legacy_prose"

POPULATIONS: tuple[str, ...] = (
    POPULATION_ASSEMBLY_SPAN,
    POPULATION_ASSESSMENT_SENTENCE,
    POPULATION_LEGACY_PROSE,
)

#: The assembly payload's schema + the regimes. Mirrored rather than imported:
#: ``assembly_payload`` pulls the whole composition tree in behind it, and this
#: module is deliberately a leaf (the same reason ``_external_audit_sampling``
#: mirrors the full-width bracket regex instead of importing ``export_api``).
ASSEMBLY_SCHEMA = "assembly.v1"
ASSESSMENT_SCHEMA = "assessment.v1"
REGIME_ASSEMBLY = "assembly"
REGIME_LEGACY = "legacy"
REGIME_ROLLUP = "rollup"


# ---------------------------------------------------------------------------
# UNCHECKABLE — the deterministic, pre-search classes (design §0.5, §1.2)
# ---------------------------------------------------------------------------

UNCHECKABLE_SCOPE_BOUNDED = "scope_bounded"
UNCHECKABLE_PROVENANCE = "provenance"
UNCHECKABLE_PERSPECTIVE = "perspective"

UNCHECKABLE_CLASSES: tuple[str, ...] = (
    UNCHECKABLE_SCOPE_BOUNDED,
    UNCHECKABLE_PROVENANCE,
    UNCHECKABLE_PERSPECTIVE,
)

#: PROVENANCE — the truth-maker is this packet. Checked FIRST, because a claim
#: about our own verification floor is *also* scope-bounded and the more
#: specific class is the honest one. Every alternative here is either an R-round
#: specimen or the shape of one.
_PROVENANCE_RE = re.compile(
    r"(?:"
    r"principal units?"
    r"|unit reads?"
    r"|verified reads?"
    r"|verification floor"
    r"|no head\b"
    r"|head(?:s)? (?:were|was) (?:not )?(?:available|resolved|admitted)"
    r"|this (?:read|packet|assessment|assembly|record|cycle)\b"
    r"|the (?:read|packet|assembly|record) (?:above|below|itself)"
    r"|desk(?:s)? (?:did not|do not|have not) (?:report|produce)"
    r"|(?:this|the) (?:analysis|system|platform)(?:'s|s')? own\b"
    r")",
    re.IGNORECASE,
)

#: SCOPE-BOUNDED — the claim is bounded to the collection/slice this desk read,
#: so the world cannot settle it. The lexicon is the design's §1.2 measurement
#: (148 / 1,619 = 9.1% together with provenance), reproduced verbatim in shape.
_SCOPE_RE = re.compile(
    r"(?:"
    r"this desk'?s? collection"
    r"|the collection\b"
    r"|in this slice"
    r"|slice covers"
    r"|the corpus\b"
    r"|as of \d"
    r"|as of [A-Z][a-z]+ \d"
    r"|within the (?:window|slice|collection|corpus)"
    r"|coverage (?:of|for|remains|is|was)"
    r"|no (?:new )?(?:signals?|items?|reporting) in (?:the|this)"
    r")",
    re.IGNORECASE,
)

#: The scope token the assembler already writes when
#: ``composition_integrity.has_collection_denominator_scope`` fires on the span's
#: SOURCE SENTENCE. When it is present we do not need the lexicon at all — the
#: producer already decided, at construction time, that this span carries a
#: collection denominator.
SCOPE_TOKEN_COLLECTION_DENOMINATOR = "collection_denominator"

#: PERSPECTIVE — the voice's own weighing. Judgement verbs, epistemic modals and
#: first-person analytic framing. Deliberately NOT a hedge detector: a hedged
#: statement about the world is still about the world.
_PERSPECTIVE_RE = re.compile(
    r"(?:"
    r"\b(?:I|we) (?:read|judge|assess|would|expect|watch|note|think)\b"
    r"|\b(?:suggests?|implies|indicates that|points to|reads as)\b"
    r"|\b(?:the )?(?:risk|question|worry|concern|implication|significance) (?:is|here)\b"
    r"|\bwhat (?:matters|to watch|would change)\b"
    r"|\b(?:most|more) (?:likely|important(?:ly)?|telling)\b"
    r"|\bon (?:balance|this reading)\b"
    r"|\bthe desks? (?:are|is|remain) thin\b"
    r"|\b(?:worth|hard) (?:watching|to read)\b"
    r"|\bmy reading\b"
    r")",
    re.IGNORECASE,
)

#: A ``[[ref:N]]`` marker — the Assessment's citation shape. A FACT sentence
#: carries at least one; a sentence naming none is not stating what a block says.
_REF_RE = re.compile(r"\[\[ref:\s*(\d+)\s*\]\]")


# ---------------------------------------------------------------------------
# The absence stratum (design §1.3) — named, never folded into the headline
# ---------------------------------------------------------------------------

#: STRICT absence — 266 / 1,619 = 16.4% live. These are the claims that cannot
#: be graded at all while the search plane cannot verify its own emptiness
#: (``supports_absence_claim: false`` on every live specimen).
_ABSENCE_RE = re.compile(
    r"(?:"
    r"not observed"
    r"|no evidence"
    r"|no new "
    r"|no reported"
    r"|none (?:of|were|was) "
    r"|no signs"
    r"|remains? (?:absent|unobserved)"
    r"|no (?:indication|confirmation|disruption|incident|escalation|change|coordinated)"
    r")",
    re.IGNORECASE,
)


def absence_shaped(text: str) -> bool:
    """True when the claim asserts a NON-event (design §1.3, strict shape).

    Its own stratum, its own decided rate, never folded into the headline: 16.4%
    of every read is an absence claim and none of it is gradable while SearXNG
    cannot verify emptiness. Folding it in would make the headline move with the
    search plane's engine health, which is exactly the pooling the D5 stamp
    doctrine forbids.
    """
    return bool(_ABSENCE_RE.search(str(text or "")))


def perspective_shaped(text: str, *, has_ordinal: bool) -> bool:
    """True when an Assessment sentence is PERSPECTIVE rather than FACT.

    See the module banner: D-6 §A2's split lives in a prompt, so this is a
    deterministic detector over the same two classes, not the prompt's rule.
    Two signals, both off the payload:

      * ``has_ordinal`` False — the sentence names no spine ordinal, so it is
        not stating what a block says;
      * a judgement / epistemic-framing marker — the sentence is an argument
        about the record.
    """
    if not has_ordinal:
        return True
    return bool(_PERSPECTIVE_RE.search(str(text or "")))


def classify_uncheckable(
    text: str, *, scope_tokens: Sequence[str] = (), perspective: bool = False
) -> str | None:
    """The deterministic pre-search class, or ``None`` when the world can decide.

    Order is precedence and the order is deliberate: ``perspective`` is a
    property of the SENTENCE KIND and settles it outright; ``provenance`` beats
    ``scope_bounded`` because a claim about our own verification floor is also
    slice-bounded and the more specific class is the honest one.
    """
    body = str(text or "")
    if perspective:
        return UNCHECKABLE_PERSPECTIVE
    if _PROVENANCE_RE.search(body):
        return UNCHECKABLE_PROVENANCE
    if any(str(t).strip() for t in (scope_tokens or ())):
        return UNCHECKABLE_SCOPE_BOUNDED
    if _SCOPE_RE.search(body):
        return UNCHECKABLE_SCOPE_BOUNDED
    return None


# ---------------------------------------------------------------------------
# The claim key — stable across replay a month later
# ---------------------------------------------------------------------------

#: The separator between claim-key components. A bare concatenation of
#: ``fold(text) || head || start || end`` is not injective — ``("ab", h, 1, 23)``
#: and ``("ab", h, 12, 3)`` collide — and a claim key that can collide is a
#: ledger row that can be overwritten by a different claim. U+001F UNIT
#: SEPARATOR cannot occur in folded prose, so it is a real fence.
_KEY_SEP = "\x1f"


def claim_key(
    text: str, origin_head_id: Any, start: Any, end: Any
) -> str:
    """``sha256(fold(text) || origin_head_id || start || end)`` — design §4 W-1.

    Folded through the plane's ONE normalisation site
    (:func:`~legba.data.provenance.text_fold.normalize_for_match`) so a U+2011
    that the renderer inserted between two replays cannot mint a second key for
    the same claim — the MECH-6 class, applied to identity rather than to a
    comparator.

    Byte-exact origin coordinates are in the key because they are what make the
    key REPLAYABLE: a month later the same date resolves the same head, the same
    offsets and therefore the same key, which is exactly what a disputed
    CONTRADICTED verdict needs in order to be re-argued.
    """
    material = _KEY_SEP.join((
        normalize_for_match(text),
        str(origin_head_id or ""),
        str(start if start is not None else ""),
        str(end if end is not None else ""),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WidthClaim:
    """ONE claim in the width population — a row of the ledger before grading.

    Every field here lands on an ``external_grades`` row (migration 0190), and
    every one of them is a stratum somebody will ask for: the A/B regime arm,
    the position-in-the-read pair (``span_role`` + ``block_ordinal``, which is
    what lets a position-weighted number be computed LATER rather than headlined
    now on a rule nobody registered), the two honest-exclusion flags, and the
    read's own evidence window, which is the admissible source window G-3
    grades against.
    """

    claim_text: str
    population: str
    graded_output_id: str
    analyst_id: str
    #: Byte/char origin coordinates. ``origin_head_id`` is the DESK head the
    #: span was cut from under the assembly, and the READ itself off-assembly.
    origin_head_id: str
    start: int
    end: int
    target_id: str | None = None
    desk_key: str = ""
    block_ordinal: int | None = None
    span_role: str | None = None
    claim_severity: str | None = None
    assembly_regime: str = REGIME_LEGACY
    scope_bounded: bool = False
    absence_shaped: bool = False
    uncheckable_class: str | None = None
    read_evidence_window: dict[str, Any] = field(default_factory=dict)
    retrieval_origin_mix: dict[str, Any] = field(default_factory=dict)
    #: True for ordinal 1 — the lead block, which the drain grades first.
    lead_block: bool = False
    #: The read's own produced_at, ISO. The refill watermark advances on it.
    produced_at: str = ""

    @property
    def key(self) -> str:
        return claim_key(self.claim_text, self.origin_head_id, self.start, self.end)

    @property
    def checkable(self) -> bool:
        return self.uncheckable_class is None

    def as_dict(self) -> dict[str, Any]:
        """The queue's on-disk shape (and the ledger writer's input)."""
        return {
            "claim_key": self.key,
            "claim_text": self.claim_text,
            "population": self.population,
            "graded_output_id": self.graded_output_id,
            "analyst_id": self.analyst_id,
            "origin_head_id": self.origin_head_id,
            "start": self.start,
            "end": self.end,
            "target_id": self.target_id,
            "desk_key": self.desk_key,
            "block_ordinal": self.block_ordinal,
            "span_role": self.span_role,
            "claim_severity": self.claim_severity,
            "assembly_regime": self.assembly_regime,
            "scope_bounded": self.scope_bounded,
            "absence_shaped": self.absence_shaped,
            "uncheckable_class": self.uncheckable_class,
            "read_evidence_window": dict(self.read_evidence_window),
            "retrieval_origin_mix": dict(self.retrieval_origin_mix),
            "lead_block": self.lead_block,
            "produced_at": self.produced_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WidthClaim":
        """Rehydrate a queued claim. Tolerant by design: the queue survives a
        code change, and a field this version does not know about is dropped
        rather than taking the drain down."""
        return cls(
            claim_text=str(raw.get("claim_text") or ""),
            population=str(raw.get("population") or POPULATION_LEGACY_PROSE),
            graded_output_id=str(raw.get("graded_output_id") or ""),
            analyst_id=str(raw.get("analyst_id") or ""),
            origin_head_id=str(raw.get("origin_head_id") or ""),
            start=int(raw.get("start") or 0),
            end=int(raw.get("end") or 0),
            target_id=raw.get("target_id"),
            desk_key=str(raw.get("desk_key") or ""),
            block_ordinal=raw.get("block_ordinal"),
            span_role=raw.get("span_role"),
            claim_severity=raw.get("claim_severity"),
            assembly_regime=str(raw.get("assembly_regime") or REGIME_LEGACY),
            scope_bounded=bool(raw.get("scope_bounded")),
            absence_shaped=bool(raw.get("absence_shaped")),
            uncheckable_class=raw.get("uncheckable_class"),
            read_evidence_window=dict(raw.get("read_evidence_window") or {}),
            retrieval_origin_mix=dict(raw.get("retrieval_origin_mix") or {}),
            lead_block=bool(raw.get("lead_block")),
            produced_at=str(raw.get("produced_at") or ""),
        )


# ---------------------------------------------------------------------------
# Row readers
# ---------------------------------------------------------------------------


def _payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """The ``analyst_outputs.data`` column as a mapping (drivers vary)."""
    data = row.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return {}
    return dict(data) if isinstance(data, Mapping) else {}


def _inner(row: Mapping[str, Any]) -> dict[str, Any]:
    """``data.data`` — the payload's own data field, one level down."""
    inner = _payload(row).get("data")
    return dict(inner) if isinstance(inner, Mapping) else {}


def read_assembly(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """``data.data.assembly`` when the row is a real assembled read.

    ``None`` for a legacy-regime row, a rollup, an unknown schema, or a row with
    the ``{schema, regime}`` stamp and nothing behind it. The regime half of the
    gate is D-3 §1.1's finding and it is not decoration: 117 live rows carry the
    stamp with ``regime='legacy'`` and no blocks, and a schema-only gate would
    "grade" every one of them.
    """
    assembly = _inner(row).get("assembly")
    if not isinstance(assembly, Mapping):
        return None
    if str(assembly.get("schema") or "") != ASSEMBLY_SCHEMA:
        return None
    regime = str(assembly.get("regime") or "")
    if regime in (REGIME_LEGACY, REGIME_ROLLUP):
        return None
    if not isinstance(assembly.get("blocks"), list):
        return None
    return dict(assembly)


def read_assessment(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """``data.data.assessment`` when the row is an Assessment-channel read."""
    assessment = _inner(row).get("assessment")
    if not isinstance(assessment, Mapping):
        return None
    if str(assessment.get("schema") or "") != ASSESSMENT_SCHEMA:
        return None
    return dict(assessment)


def read_regime(row: Mapping[str, Any]) -> str:
    """The row's assembly regime — the A/B ARM, stamped on every ledger row.

    ``legacy`` when the row predates the stamp, so the arm is a closed
    vocabulary rather than a nullable field nobody can group by.
    """
    assembly = _inner(row).get("assembly")
    if isinstance(assembly, Mapping):
        regime = str(assembly.get("regime") or "").strip()
        if regime:
            return regime
    if read_assessment(row) is not None:
        return REGIME_ASSEMBLY
    return REGIME_LEGACY


def _evidence_window(row: Mapping[str, Any]) -> dict[str, Any]:
    """The read's OWN admissible source window (G-3). Never the grader's guess.

    R3 §4.7: *"Every declared span is 1–2 days inside a read graded and consumed
    as a 14-day country read"* — the window is a property of the read, and a
    grader asked to judge recency without it will invent one.
    """
    window = _inner(row).get("evidence_window")
    return dict(window) if isinstance(window, Mapping) else {}


def _retrieval_origin_mix(row: Mapping[str, Any]) -> dict[str, Any]:
    """The read's cited signals by ``retrieval_origin`` — RECORDED, not shown.

    G-5: the grader never sees the read's own citations, because handing it the
    [N] sources collapses this instrument into faithfulness and re-creates the
    84% problem inside the thing built to fix it. The mix rides the ledger row
    for the A/B stratum, and it is honest-null today: ``retrieval_origin`` is
    NULL on all 23,774 signals fetched in the last 7 days.
    """
    mix = _inner(row).get("retrieval_origin_mix")
    return dict(mix) if isinstance(mix, Mapping) else {}


def _severity_from_tags(row: Mapping[str, Any]) -> str | None:
    for tag in _payload(row).get("tags") or []:
        text = str(tag)
        if text.startswith("severity:"):
            value = text.split(":", 1)[1].strip()
            return value or None
    return None


def _iso(value: Any) -> str:
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


# ---------------------------------------------------------------------------
# W-1 — the two enumerators
# ---------------------------------------------------------------------------


def claims_from_assembly(row: Mapping[str, Any]) -> list[WidthClaim]:
    """Every ``blocks[].spans[]`` of an assembled read, as claims.

    Deterministic and total: the enumeration order is (block ordinal, span
    index), which is the payload's own order, so two runs over the same row
    produce the same list in the same order with the same keys. No model is
    consulted and none can be — the population is a field.

    The span's ``origin.head_id`` is the DESK head it was cut from (D-1 §0.3's
    depth-1 rule), which is why ``origin_head_id`` on the claim is not the read:
    grading this claim grades that desk, and the ledger has to be able to say
    which one.
    """
    assembly = read_assembly(row)
    if assembly is None:
        return []
    regime = str(assembly.get("regime") or REGIME_ASSEMBLY)
    window = _evidence_window(row)
    origin_mix = _retrieval_origin_mix(row)
    output_id = str(row.get("id") or "")
    analyst_id = str(row.get("analyst_id") or "")
    produced_at = _iso(row.get("produced_at"))
    out: list[WidthClaim] = []
    for block in assembly.get("blocks") or []:
        if not isinstance(block, Mapping):
            continue
        ordinal = block.get("ordinal")
        try:
            ordinal_int = int(ordinal) if ordinal is not None else None
        except (TypeError, ValueError):
            ordinal_int = None
        for span in block.get("spans") or []:
            if not isinstance(span, Mapping):
                continue
            # P3-A: a COUNTRY block additionally carries its origin desk head in
            # FULL under ``context_body``. The ``assembly_span`` population is
            # "the desk SENTENCES the record quotes" — one claim per quoted
            # span, priced and audited as one. A whole desk read is not one
            # claim; enqueuing it would put a multi-paragraph body in front of a
            # PAID auditor as a single width claim and charge the population for
            # it. Skipped by ROLE, and stated rather than hidden: it is not that
            # the context cannot be checked, it is that it is not a claim.
            if str(span.get("role") or "") in CONTEXT_SPAN_ROLES:
                continue
            text = str(span.get("text") or "").strip()
            if not text:
                continue
            origin = span.get("origin")
            origin = dict(origin) if isinstance(origin, Mapping) else {}
            scope_tokens = [
                str(t) for t in (span.get("scope_tokens") or []) if str(t).strip()
            ]
            out.append(WidthClaim(
                claim_text=text,
                population=POPULATION_ASSEMBLY_SPAN,
                graded_output_id=output_id,
                analyst_id=analyst_id,
                origin_head_id=str(origin.get("head_id") or block.get("finding_id") or ""),
                start=int(origin.get("start") or 0),
                end=int(origin.get("end") or 0),
                target_id=(
                    str(block.get("target_id"))
                    if block.get("target_id") is not None else None
                ),
                desk_key=str(block.get("desk") or analyst_id),
                block_ordinal=ordinal_int,
                span_role=(
                    str(span.get("role")) if span.get("role") is not None else None
                ),
                claim_severity=(
                    str(block.get("severity"))
                    if block.get("severity") else _severity_from_tags(row)
                ),
                assembly_regime=regime,
                scope_bounded=bool(scope_tokens),
                absence_shaped=absence_shaped(text),
                uncheckable_class=classify_uncheckable(
                    text, scope_tokens=scope_tokens
                ),
                read_evidence_window=window,
                retrieval_origin_mix=origin_mix,
                lead_block=(ordinal_int == 1),
                produced_at=produced_at,
            ))
    return out


def claims_from_assessment(
    row: Mapping[str, Any], *, segmenter: Any = None
) -> list[WidthClaim]:
    """Every sentence of an Assessment-channel read, as claims.

    The VOICE population. Its origin head is the SPINE the Assessment was
    written from (``assessment.spine_id``), because that is the record this
    prose is answerable to and the thing a replay resolves.

    ``segmenter`` defaults to ``assessment_unsupported.segment_sentences`` — the
    same paragraph- and abbreviation-aware segmentation the spine's lead spans
    were cut with, so "U.S." does not end a sentence in one module and not the
    other. Injected so this module stays importable without the composition
    tree, and so a caller can prove the split it got.
    """
    assessment = read_assessment(row)
    if assessment is None:
        return []
    body = str(row.get("body") or "")
    if not body.strip():
        return []
    if segmenter is None:
        try:
            from ..assessment_unsupported import segment_sentences as segmenter
        except Exception as exc:  # pragma: no cover — import guard
            logger.warning(
                "external_audit.assessment_segmenter_unavailable err=%s — the "
                "voice population is skipped this tick rather than split by a "
                "second, disagreeing sentence rule", exc,
            )
            return []
    window = _evidence_window(row)
    origin_mix = _retrieval_origin_mix(row)
    output_id = str(row.get("id") or "")
    analyst_id = str(row.get("analyst_id") or "")
    spine_id = str(assessment.get("spine_id") or "")
    severity = _severity_from_tags(row)
    produced_at = _iso(row.get("produced_at"))
    out: list[WidthClaim] = []
    for index, sentence in enumerate(segmenter(body), start=1):
        text = str(getattr(sentence, "text", "") or "").strip()
        if not text:
            continue
        start = int(getattr(sentence, "start", 0) or 0)
        end = int(getattr(sentence, "end", 0) or 0)
        has_ordinal = bool(_REF_RE.search(text))
        out.append(WidthClaim(
            claim_text=text,
            population=POPULATION_ASSESSMENT_SENTENCE,
            graded_output_id=output_id,
            analyst_id=analyst_id,
            origin_head_id=spine_id,
            start=start,
            end=end,
            target_id=(
                str(row.get("target_id"))
                if row.get("target_id") is not None else None
            ),
            desk_key=str(row.get("target_id") or analyst_id or "world"),
            block_ordinal=index,
            span_role="sentence",
            claim_severity=severity,
            assembly_regime=REGIME_ASSEMBLY,
            scope_bounded=False,
            absence_shaped=absence_shaped(text),
            uncheckable_class=classify_uncheckable(
                text,
                perspective=perspective_shaped(text, has_ordinal=has_ordinal),
            ),
            read_evidence_window=window,
            retrieval_origin_mix=origin_mix,
            lead_block=(index == 1),
            produced_at=produced_at,
        ))
    return out


def claims_from_read(
    row: Mapping[str, Any], *, segmenter: Any = None
) -> list[WidthClaim]:
    """Dispatch on what the row IS — assembly, Assessment, or neither.

    A legacy-regime row returns ``[]`` and the caller keeps today's LLM
    extraction leg for it. That branch is the whole reason the loop still works
    with ``LEGBA_COMPOSITION_ASSEMBLY=0``.
    """
    if read_assembly(row) is not None:
        return claims_from_assembly(row)
    if read_assessment(row) is not None:
        return claims_from_assessment(row, segmenter=segmenter)
    return []


# ---------------------------------------------------------------------------
# The query — deterministic formulation (design §1.4)
# ---------------------------------------------------------------------------

#: Stripped before the noun phrase is cut. Not a linguistic stopword list — a
#: SEARCH one: words that cost query budget and buy no discrimination.
_QUERY_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "for",
    "from", "had", "has", "have", "in", "is", "it", "its", "of", "on", "or",
    "that", "the", "their", "there", "this", "to", "was", "were", "which",
    "with", "would",
})

#: Marker/citation shapes the model or the assembler wrote INTO the prose. They
#: are ours, not the world's, and a search engine handed one returns nothing.
_QUERY_NOISE_RE = re.compile(r"(?:\[\[ref:\s*\d+\s*\]\]|\[\d+\]|【\s*\d+\s*】)")

#: Query length cap — the ``query`` column and every SERP provider's own bound.
QUERY_MAX_CHARS = 400
#: How many content words of the claim reach the query. Enough to name the
#: actor and the act; short enough that the engine matches on it.
QUERY_MAX_TERMS = 14


def build_query(claim: WidthClaim) -> str:
    """The search query for one claim — deterministic, replayable, no model.

    ``target + claim noun phrase + window`` (design §1.4). Deterministic because
    the whole instrument is: a query a model wrote cannot be replayed against
    the same engine a month later, and a verdict whose query cannot be replayed
    is a verdict that cannot be re-argued.
    """
    text = _QUERY_NOISE_RE.sub(" ", str(claim.claim_text or ""))
    words: list[str] = []
    for raw in re.split(r"\s+", text):
        word = raw.strip().strip(".,;:!?()[]{}\"'“”‘’")
        if not word:
            continue
        if word.lower() in _QUERY_STOPWORDS:
            continue
        words.append(word)
        if len(words) >= QUERY_MAX_TERMS:
            break
    parts: list[str] = []
    target = (claim.target_id or "").strip()
    if target and target.lower() not in {w.lower() for w in words}:
        parts.append(target)
    parts.extend(words)
    window = _window_hint(claim.read_evidence_window)
    if window:
        parts.append(window)
    return " ".join(parts)[:QUERY_MAX_CHARS].strip()


def _window_hint(window: Mapping[str, Any]) -> str:
    """``YYYY-MM`` off the read's own window latest edge, or ``""``.

    Month grain, not day: a day pin turns a legitimate two-day-late report into
    a NOT_FOUND, and the ACTUAL admissible-window test is G-3's, run against the
    source's publication date after the search — not smuggled into the query.

    ``newest`` FIRST because it is the key the production stamp actually uses
    (``composition_window.evidence_window_span``); ``latest`` is
    ``region_rollup``'s spelling and ``to`` the API window object's. Reading only
    the last two meant this hint was empty on every real width read, so no query
    carried a month at all.
    """
    latest = (
        window.get("newest") or window.get("latest") or window.get("to")
        or window.get("oldest") or window.get("earliest")
    )
    text = _iso(latest)
    return text[:7] if len(text) >= 7 and text[4] == "-" else ""


# ---------------------------------------------------------------------------
# Counting — the pre-filter's own report
# ---------------------------------------------------------------------------


def prefilter_counts(claims: Sequence[WidthClaim]) -> dict[str, int]:
    """The pre-filter's own numbers, reported beside the verdict mix.

    §1.2's argument in a dict: a loop that silently swallows the uncheckable
    class produces a deflated headline and no way to see why.
    """
    counts = {
        "claims": len(claims),
        "searchable": 0,
        "absence_shaped": 0,
    }
    for cls in UNCHECKABLE_CLASSES:
        counts[f"uncheckable_{cls}"] = 0
    for claim in claims:
        if claim.absence_shaped:
            counts["absence_shaped"] += 1
        if claim.uncheckable_class is None:
            counts["searchable"] += 1
        else:
            counts[f"uncheckable_{claim.uncheckable_class}"] += 1
    return counts


def iter_unique(claims: Sequence[WidthClaim]) -> Iterator[WidthClaim]:
    """Claims with duplicate keys collapsed, first occurrence winning.

    D-5 measured that 34 of 64 world-assembly spans (53%) resolve to a desk-head
    span one hop down — so ~8.5 world spans/day are re-quotations of country
    spans already in the population. Grading both would double-count the same
    desk sentence in the same day's rate.
    """
    seen: set[str] = set()
    for claim in claims:
        key = claim.key
        if key in seen:
            continue
        seen.add(key)
        yield claim


__all__ = [
    "ASSEMBLY_SCHEMA",
    "ASSESSMENT_SCHEMA",
    "POPULATIONS",
    "POPULATION_ASSEMBLY_SPAN",
    "POPULATION_ASSESSMENT_SENTENCE",
    "POPULATION_LEGACY_PROSE",
    "QUERY_MAX_CHARS",
    "REGIME_ASSEMBLY",
    "REGIME_LEGACY",
    "REGIME_ROLLUP",
    "SCOPE_TOKEN_COLLECTION_DENOMINATOR",
    "UNCHECKABLE_CLASSES",
    "UNCHECKABLE_PERSPECTIVE",
    "UNCHECKABLE_PROVENANCE",
    "UNCHECKABLE_SCOPE_BOUNDED",
    "WidthClaim",
    "absence_shaped",
    "build_query",
    "claim_key",
    "claims_from_assembly",
    "claims_from_assessment",
    "claims_from_read",
    "classify_uncheckable",
    "iter_unique",
    "perspective_shaped",
    "prefilter_counts",
    "read_assembly",
    "read_assessment",
    "read_regime",
]
