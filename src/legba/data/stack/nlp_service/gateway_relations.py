# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fact quality on the gateway's ``/relations`` answer (W11-F113) — the pure parts.

The first gateway facts (2026-10-23) read as junk at the 0.5 floor ("Melania
Trump | operates in | Oleshky", "truce | signed agreement with | Oleshky"):
every hit was kept, none carried a relation score, and half the labels asked
for named relations nothing downstream reads. Three knobs fix that on the
client side, all component config (``nlp.innoai.gateway``), none an env var:

  * **confidence** (``relation_confidence``) — the triple's ``confidence`` is
    the ``min`` (default), ``mean`` or ``none`` (no key: the fact path's 0.5
    floor, the pre-F113 behaviour) of the head and tail scores.
  * **threshold** (``relation_threshold``, default 0.5) — sent to the gateway
    as ``threshold`` AND applied here to the same aggregate, so a hit the
    server keeps on a different score (or a server that ignores the field)
    still cannot land below it. ``0`` turns it off.
  * **vocabulary** (``relation_labels``) — the default label set is DERIVED
    from the graph's relationship types (:func:`derive_relation_labels`), not
    the 20 generic GLiREL labels legba-models asked for.

Plus a cheap junk gate (:func:`junk_reason`): head and tail must be different
strings, each at least two characters and not a bare stopword.

Nothing here logs and nothing here touches the network.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from ...vocabulary import RELATIONSHIP_TYPES, normalize_predicate

# ---------------------------------------------------------------------------
# Vocabulary — the labels the graph consumes
# ---------------------------------------------------------------------------

#: The 20 generic GLiREL labels legba-models' ``/extract`` asked for when the
#: caller sent none (legba-models/app/main.py ``DEFAULT_RELATION_LABELS``) —
#: the gateway's default until W11-F113. Kept for the record and for an
#: operator who wants them back (``relation_labels`` on the component).
LEGACY_GLIREL_RELATION_LABELS: tuple[str, ...] = (
    "leader of",
    "member of",
    "located in",
    "headquarters in",
    "founded by",
    "part of",
    "ally of",
    "opponent of",
    "conflict with",
    "signed agreement with",
    "sanctioned by",
    "spokesperson for",
    "operates in",
    "supplies to",
    "border with",
    "capital of",
    "controls",
    "parent organization of",
    "subsidiary of",
    "employed by",
)

#: Graph relationship types a span-relation model is never asked for.
#: ``CoOccursWith`` is the generic fallback edge (``_fact_graph`` writes it for
#: any predicate it cannot type, ``entity_resolution`` lands co-mentions as it);
#: asking an extractor "does A co-occur with B" types nothing.
NON_EXTRACTABLE_RELATIONSHIP_TYPES: frozenset[str] = frozenset({"CoOccursWith"})


def _split_camel(name: str) -> str:
    out: list[str] = []
    for ch in name:
        if ch.isupper() and out:
            out.append(" ")
        out.append(ch.lower())
    return "".join(out)


def derive_relation_labels(
    relationship_types: Iterable[str] = RELATIONSHIP_TYPES,
) -> tuple[str, ...]:
    """The relation labels the graph consumes, in its own row-predicate form.

    Each relationship type (``legba.data.vocabulary.RELATIONSHIP_TYPES`` — the
    seed of ``GET /registry/vocabulary/relationship_type``, and the set the
    reifier's ``ALLOWED_REL_TYPES`` / structural-balance ``POLARITY`` sign) is
    mapped through :func:`normalize_predicate`, the write path's own predicate
    canon, so the label GLiREL answers with IS the ``facts.predicate`` the row
    keys on (``LeaderOf`` → ``"leader of"``). A type the canon does not map
    (a future registry addition) is split from its CamelCase. Types in
    :data:`NON_EXTRACTABLE_RELATIONSHIP_TYPES` are skipped; order is the
    vocabulary's, duplicates dropped.
    """
    out: list[str] = []
    for rel_type in relationship_types:
        if rel_type in NON_EXTRACTABLE_RELATIONSHIP_TYPES:
            continue
        label = normalize_predicate(rel_type)
        if label == rel_type:
            label = _split_camel(rel_type)
        label = " ".join(label.split()).casefold()
        if label and label not in out:
            out.append(label)
    return tuple(out)


#: The default ``relation_labels`` on the gateway — see
#: :func:`derive_relation_labels`. Thirteen labels: every graph relationship
#: type less ``CoOccursWith``.
DEFAULT_RELATION_LABELS: tuple[str, ...] = derive_relation_labels()

# ---------------------------------------------------------------------------
# Confidence + threshold
# ---------------------------------------------------------------------------

CONFIDENCE_MIN = "min"
CONFIDENCE_MEAN = "mean"
CONFIDENCE_NONE = "none"
RELATION_CONFIDENCE_MODES: tuple[str, ...] = (
    CONFIDENCE_MIN, CONFIDENCE_MEAN, CONFIDENCE_NONE,
)
#: ``min``: a relation is only as sure as its weaker end. The gateway scores
#: the two endpoint SPANS, not the relation, and "Melania Trump operates in
#: Oleshky" is wrong if either span is wrong — a confident head cannot vouch
#: for a doubtful tail. ``mean`` would let one strong end lift a junk partner
#: over the floor; ``min`` never does.
DEFAULT_RELATION_CONFIDENCE = CONFIDENCE_MIN
#: Sent as ``threshold`` and applied client-side to the aggregate. 0.5 is the
#: fact path's own floor (``_INGESTION_DEFAULT_CONFIDENCE``): a gateway triple
#: below it would land LESS certain than an unscored one, so it is not kept.
DEFAULT_RELATION_THRESHOLD = 0.5


def aggregate_confidence(
    subject_conf: float | None, object_conf: float | None, mode: str,
) -> float | None:
    """The relation score from its two endpoint scores, or ``None``.

    ``None`` when either endpoint is unscored (a plain ``[head, tail]`` pair),
    or the mode is ``none`` (the triple then carries no ``confidence`` and the
    fact path applies its documented floor).
    """
    if subject_conf is None or object_conf is None or mode == CONFIDENCE_NONE:
        return None
    if mode == CONFIDENCE_MEAN:
        value = (subject_conf + object_conf) / 2.0
    else:
        value = min(subject_conf, object_conf)
    return round(max(0.0, min(1.0, value)), 4)


def gate_score(subject_conf: float | None, object_conf: float | None) -> float | None:
    """The score the threshold applies to under mode ``none`` — the min, the
    conservative aggregate — so turning the stamp off never loosens the gate."""
    return aggregate_confidence(subject_conf, object_conf, CONFIDENCE_MIN)


# ---------------------------------------------------------------------------
# Junk gate
# ---------------------------------------------------------------------------

#: Function words a span model returns as an endpoint ("he", "it", "the") —
#: never an entity. Short and English-only on purpose: the gateway answers on
#: the (translated-to-English) text, and a real name is never one of these.
ENDPOINT_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "there", "here",
    "he", "she", "it", "its", "they", "them", "their", "theirs", "his", "her",
    "hers", "him", "we", "us", "our", "ours", "i", "me", "my", "mine", "you",
    "your", "yours", "who", "whom", "whose", "which", "what", "one", "ones",
    "and", "or", "but", "of", "in", "on", "at", "to", "for", "by", "with",
    "from", "as", "is", "was", "are", "were", "be", "been", "said", "says",
    "also", "other", "others", "some", "all", "both", "each", "many", "most",
    "itself", "himself", "herself", "themselves", "someone", "something",
})

_STRIP = " \t\n\r\"'`.,;:!?()[]{}<>«»“”‘’-"


def _norm_endpoint(text: str) -> str:
    return " ".join(str(text).split()).strip(_STRIP).casefold()


def junk_reason(subject: str, obj: str) -> str | None:
    """Why a (head, tail) pair is junk, or ``None`` when it may pass.

    ``same_endpoint`` — head and tail are the same string (case, spacing and
    edge punctuation ignored); ``short_endpoint`` — either is under two
    characters once stripped; ``stopword_endpoint`` — either is a bare
    function word (an ALL-CAPS acronym — "US", "WHO" — is not one). The fact
    path's deeper gates (demonyms, quantities, inversions, rosters) still run
    after this; this only stops the cheapest junk before it costs a
    fact-write round.
    """
    s, o = _norm_endpoint(subject), _norm_endpoint(obj)
    if len(s) < 2 or len(o) < 2:
        return "short_endpoint"
    if s == o:
        return "same_endpoint"
    if _is_stopword(subject, s) or _is_stopword(obj, o):
        return "stopword_endpoint"
    return None


def _is_stopword(raw: str, norm: str) -> bool:
    """A bare function word — unless the surface is an ALL-CAPS acronym:
    "US", "WHO", "IT" are entities, "us", "who", "it" are not."""
    if norm not in ENDPOINT_STOPWORDS:
        return False
    return not str(raw).strip(_STRIP).isupper()


def apply_relation_quality(
    triples: Sequence[Mapping[str, Any]],
    *,
    confidence_mode: str = DEFAULT_RELATION_CONFIDENCE,
    threshold: float | None = DEFAULT_RELATION_THRESHOLD,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Junk gate, confidence stamp and threshold over mapped triples.

    Returns ``(kept, counts)`` where ``counts`` has ``hits``, ``kept``,
    ``dropped_junk`` and ``dropped_below_threshold``. Order: junk first (a
    junk pair is counted as junk whatever its score), then the threshold on
    the aggregate (the ``min`` under mode ``none``, see :func:`gate_score`).
    A triple with an unscored endpoint is never dropped by the threshold —
    there is no score to hold it to; it keeps the fact path's floor.
    ``subject_confidence`` / ``object_confidence`` ride along unchanged.
    """
    kept: list[dict[str, Any]] = []
    counts = {"hits": len(triples), "kept": 0, "dropped_junk": 0,
              "dropped_below_threshold": 0}
    for triple in triples:
        if junk_reason(str(triple.get("subject", "")), str(triple.get("object", ""))):
            counts["dropped_junk"] += 1
            continue
        s_conf = triple.get("subject_confidence")
        o_conf = triple.get("object_confidence")
        conf = aggregate_confidence(s_conf, o_conf, confidence_mode)
        score = conf if confidence_mode != CONFIDENCE_NONE else gate_score(s_conf, o_conf)
        if threshold and score is not None and score < threshold:
            counts["dropped_below_threshold"] += 1
            continue
        out = dict(triple)
        if conf is not None:
            out["confidence"] = conf
        kept.append(out)
    counts["kept"] = len(kept)
    return kept, counts


def coerce_confidence_mode(value: Any) -> str:
    """A configured mode, case-folded; absent → the default; unknown → error."""
    if value is None:
        return DEFAULT_RELATION_CONFIDENCE
    mode = str(value).strip().casefold()
    if mode not in RELATION_CONFIDENCE_MODES:
        raise ValueError(
            f"relation_confidence must be one of {', '.join(RELATION_CONFIDENCE_MODES)} "
            f"(got {value!r})"
        )
    return mode


def coerce_threshold(value: Any) -> float | None:
    """A configured threshold in [0, 1]; absent → the default; 0 → off (None)."""
    if value is None:
        return DEFAULT_RELATION_THRESHOLD
    if isinstance(value, bool):
        raise ValueError(f"relation_threshold must be a number in [0, 1] (got {value!r})")
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"relation_threshold must be a number in [0, 1] (got {value!r})"
        ) from exc
    if not 0.0 <= f <= 1.0:
        raise ValueError(f"relation_threshold must be in [0, 1] (got {f})")
    return f or None


__all__ = [
    "CONFIDENCE_MEAN",
    "CONFIDENCE_MIN",
    "CONFIDENCE_NONE",
    "DEFAULT_RELATION_CONFIDENCE",
    "DEFAULT_RELATION_LABELS",
    "DEFAULT_RELATION_THRESHOLD",
    "ENDPOINT_STOPWORDS",
    "LEGACY_GLIREL_RELATION_LABELS",
    "NON_EXTRACTABLE_RELATIONSHIP_TYPES",
    "RELATION_CONFIDENCE_MODES",
    "aggregate_confidence",
    "apply_relation_quality",
    "coerce_confidence_mode",
    "coerce_threshold",
    "derive_relation_labels",
    "gate_score",
    "junk_reason",
]
