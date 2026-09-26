# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult answer's PROVENANCE CENSUS (7g-2 §6) — what the answer rests on.

THE DEFECT THIS CLOSES, in the design note's own words: *"Consult correctly
flags a ten-year comparison as mostly model knowledge, and the capture's rule
is to keep that behaviour and give it something to cite."* The flag was a
judgement a reader had to take on trust. This is the count behind it:

    cited: 6 live · 2 history · 0 web · model knowledge: 3 sentences

Two measurements, deliberately of two different things, because collapsing
them is how a share stops meaning anything:

  * **the CITED REFS, by origin class.** Every consult citation is a bare
    substrate uuid, so the class comes from the ROW — ``signals.origin_class``
    and ``observations.origin_class``, read by
    ``SubstrateQueryPort.classify_cited_refs`` — never from which tool
    returned it, which would be a guess dressed as provenance. The three
    HISTORY classes (``archive`` / ``backfill_native`` /
    ``backfill_reconstructed``) fold into one ``history`` bucket, because the
    question a reader is asking is "is this a curated past or a live feed",
    not which loader wrote it.
  * **the SENTENCES with nothing behind them.** Reused from
    ``assessment_unsupported._uncited_marks`` — ONE definition of "a sentence
    that names no evidence is not a claim the record warrants", shared with
    the Assessment channel rather than copied.

WHAT "UNCITED" MEANS ON *THIS* PATH, stated plainly because it is not the same
as on the Assessment path. A consult answer carries no ``[[ref:N]]`` ordinals
at all: its final-reply contract is a ``cited_refs:`` line of bare uuids after
the ``<<<FINAL>>>`` sentinel, and the prose itself is unmarked. Grading it with
the ordinal rule alone would mark EVERY sentence uncited and the census would
be a constant. So a sentence counts as CITED here when it carries an ordinal
marker (a model that writes one is taken at its word) **or** names one of the
answer's own cited refs verbatim — the only sentence-level citation signal that
exists on this path. Everything else is ``model_knowledge``, and that is the
honest reading: the platform cannot tell you which evidence that sentence
rests on, because the sentence does not say.

NEVER A FABRICATED ZERO. When the census cannot be measured — a deps bundle
with no classifier wired, a read that failed — the per-class counts are
``None``, not ``0``, and ``basis`` says which. A zero here would read as "this
answer cites no live reporting", which is a claim; absence is not.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "CENSUS_VERSION",
    "HISTORY_ORIGIN_CLASSES",
    "build_provenance_census",
    "classify_by_origin_class",
    "count_model_knowledge_sentences",
]

#: The census's own version stamp — carried on the wire so a reader surface
#: can tell a census it understands from one it does not.
CENSUS_VERSION: str = "2026-09/7g-2"

#: The three history classes, folded into one ``history`` bucket. Spelled here
#: rather than imported from ``provenance.origin`` as "everything not in
#: LIVE_CLASSES", because that phrasing would silently absorb a seventh class
#: the day one is added, and a census must not quietly reclassify anything.
HISTORY_ORIGIN_CLASSES: frozenset[str] = frozenset(
    {"archive", "backfill_native", "backfill_reconstructed"}
)

#: The buckets the census reports, in render order. ``model_knowledge`` is a
#: SENTENCE count and the other four are REF counts — the wire keeps them in
#: one object because they answer one question together, and the panel line
#: prints the unit on each so they are never added up.
_REF_BUCKETS: tuple[str, ...] = ("live", "web_retrieval", "history", "seed")

#: An ordinal marker in a sentence — either convention. A model that writes one
#: is taken at its word; the census counts the sentence as cited and does not
#: try to resolve it (resolution is the verify plane's job, not a census's).
_ORDINAL_RE = re.compile(r"\[\[ref:\d+\]\]|\[\d+\]")

#: A bare uuid inside a sentence. The only sentence-level citation signal the
#: consult contract actually produces.
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def classify_by_origin_class(
    classification: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Fold a ``classify_cited_refs`` answer into the census's four buckets.

    ``None`` in every bucket when there is nothing to fold — the honest
    "not measured", never four zeros.
    """
    if not isinstance(classification, Mapping):
        return {bucket: None for bucket in _REF_BUCKETS} | {
            "unresolved": None,
            "cited_total": None,
        }
    by_class = classification.get("by_origin_class")
    by_class = by_class if isinstance(by_class, Mapping) else {}
    counts: dict[str, Any] = {bucket: 0 for bucket in _REF_BUCKETS}
    for klass, n in by_class.items():
        name = str(klass)
        try:
            value = int(n)
        except (TypeError, ValueError):
            continue
        if name in HISTORY_ORIGIN_CLASSES:
            counts["history"] += value
        elif name in counts:
            counts[name] += value
        else:
            # A class the census does not name is NOT folded into a bucket it
            # might not belong to. It is counted as unclassified and reported,
            # so a vocabulary change shows up as a number rather than as a
            # silent reassignment.
            counts["unclassified"] = counts.get("unclassified", 0) + value
    counts["unresolved"] = int(classification.get("unresolved") or 0)
    counts["cited_total"] = int(classification.get("asked") or 0)
    return counts


def count_model_knowledge_sentences(
    answer: str, cited_refs: Sequence[str]
) -> tuple[int, int]:
    """``(model_knowledge_sentences, sentences_examined)`` for one answer.

    Reuses ``assessment_unsupported``'s own uncited rule so the two channels
    agree on what a claim-with-nothing-behind-it is. The import is DEFERRED:
    ``assessment_unsupported`` reaches ``consult_on_demand`` through the
    prompt/payload chain, so a module-scope import here would close that cycle
    at the runtime's first analyst import. Deferring costs one cached lookup.

    ``(0, 0)`` for an empty answer, and ``(0, 0)`` — never a guessed count —
    if the classifier cannot be reached at all.
    """
    text = str(answer or "")
    if not text.strip():
        return 0, 0
    try:
        from .assessment_unsupported import _uncited_marks, segment_sentences
    except Exception as exc:  # pragma: no cover — import-time only
        logger.warning("consult_census.classifier_unavailable err=%s", exc)
        return 0, 0

    known = {str(r).strip().lower() for r in cited_refs or () if str(r).strip()}
    uncited = 0
    sentences = segment_sentences(text)
    for sentence in sentences:
        # The ordinals THIS sentence names. On the consult path the contract
        # produces none, so a verbatim cited-ref uuid stands in — see the
        # module note. A non-empty list makes `_uncited_marks` return [],
        # which is exactly the rule we are reusing rather than restating.
        ordinals: list[int] = []
        if _ORDINAL_RE.search(sentence.text):
            ordinals.append(1)
        elif known and any(
            m.group(0).lower() in known for m in _UUID_RE.finditer(sentence.text)
        ):
            ordinals.append(1)
        uncited += len(_uncited_marks(text, sentence, ordinals))
    return uncited, len(sentences)


def build_provenance_census(
    *,
    answer: str,
    cited_refs: Sequence[str],
    classification: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The whole census, server-composed, ready for the wire.

    The panel prints what this says; it computes nothing of its own. That is
    the point — a share the client derived could disagree with the answer it
    is printed beside.
    """
    counts = classify_by_origin_class(classification)
    model_knowledge, examined = count_model_knowledge_sentences(answer, cited_refs)
    census: dict[str, Any] = {
        "version": CENSUS_VERSION,
        "model_knowledge": model_knowledge,
        "sentences_examined": examined,
        "basis": (
            "cited refs classified by their own origin_class column"
            if isinstance(classification, Mapping)
            else "origin class not measured for this run"
        ),
    }
    census.update(counts)
    return census
