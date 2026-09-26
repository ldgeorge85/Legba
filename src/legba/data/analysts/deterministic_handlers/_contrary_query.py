# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE COUNTER-QUERY — what you would type to find the claim's strongest
opposition, and the guard that stops it being the claim again.

WHY THIS IS THE HARD PART. Everything else in a contrary-evidence pass is
plumbing the platform already owns: the claim population is a FIELD on the
published record, the search runs through the granted pack, the fetch runs
through the auditor's own fences. The one genuinely new act is FORMULATING THE
OPPOSING PROPOSITION, and the capture names its failure mode by name — "is the
counter-query actually the strongest opposing proposition, or a paraphrase of
the claim?". A paraphrase is not a weak counter-query; it is the ABSENCE of one
wearing its clothes, because it retrieves exactly the corroboration the standing
auditor already retrieves and then gets counted as a contrary pass that ran.

So this module has two legs and a gate between them.

THE DETERMINISTIC LEG (preferred, and it is preferred for a reason). When a
claim takes a side in R2's closed polarity vocabulary — the calibrated
open/closed, ceasefire/fighting, sanctioned/lifted table in
:mod:`legba.data.analysts.claim_contradiction` — its negation is not a matter of
opinion. "The Strait of Hormuz remains effectively shut" lands on ``closure`` at
``+1``; the opposing proposition is the ``-1`` side of the same group over the
same subject, and the query writes itself: the claim's own subject tokens plus
the opposite side's headwords. No model, no temperature, no reply to validate,
and a counter-query that is replayable from the claim text alone a month later.

THE MODEL LEG (the fallback, fenced). Most published claims take no side in that
vocabulary — the table is deliberately narrow, and widening it is how R2's first
sweep produced 57 false pairs across 24 of 32 desks. For those, one bounded
core-plane call proposes the query. The prompt states, three times and in three
registers, that the reply is a SEARCH QUERY and never a verdict, and that the
model may not assert a fact. That is a prompt, i.e. a request; the enforcement
is :func:`validate_model_query` below, which re-derives everything in code.

THE GATE. :func:`novel_tokens` counts the content words the query carries that
the claim does not. Zero is a paraphrase and the query is REFUSED before it is
ever issued — no search is spent, and the claim is recorded ``search_failed``
with reason ``counter_query_paraphrase``, which is an honest countable outcome
rather than a silent corroboration run. The count itself is written to
``claim_contentions.query_novel_tokens`` on every row, so the risk the capture
names is a column somebody can average, not an anxiety somebody can assert.

WHAT THIS MODULE NEVER DOES. It never asks a model whether a claim is true, how
likely it is, or what the evidence shows. It never lets a model's prose reach a
stored row. The only thing that survives a model call here is a string that gets
typed into a search engine, and a string that fails the gate does not even
survive that far.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..claim_contradiction import (
    MAX_CLAIM_CHARS,
    polarity_of,
    polarity_side_terms,
    proper_tokens_of,
    subject_tokens_of,
    tokenize,
)
from ._external_audit_sampling import (
    normalize_core_plane_text,
    parse_strict_json_object,
)

logger = logging.getLogger(__name__)

#: ``query_source`` values, and the table's CHECK vocabulary.
QUERY_SOURCE_POLARITY = "polarity"
QUERY_SOURCE_MODEL = "model"
QUERY_SOURCE_NONE = "none"

#: Why no query was issued. A closed set, because "we could not formulate a
#: counter-query" and "we formulated one and the search failed" must stratify a
#: week of rows apart — the audit's own UNCHECKED/NOT_FOUND lesson, one step
#: earlier in the pipeline.
REASON_NO_LLM = "counter_query_no_llm"
REASON_MODEL_EMPTY = "counter_query_model_declined"
REASON_MODEL_FAILED = "counter_query_model_failed"
REASON_PARAPHRASE = "counter_query_paraphrase"
REASON_NO_SUBJECT = "counter_query_no_subject"

#: The counter-query's subject half: how many of the claim's identity tokens
#: ride along. Six is R2's own ``subject`` cap on a rendered contradiction, kept
#: so the two surfaces describe a subject the same width.
MAX_QUERY_SUBJECT_TOKENS = 6

#: ...and its ceiling, so a pathological claim cannot produce a query the search
#: plane will truncate somewhere we cannot see.
MAX_QUERY_CHARS = 400

#: One bounded call. The vLLM handler drops ``max_tokens`` from the wire by
#: default (a self-hosted server serves its own budget), so this is the
#: hosted-endpoint safety net rather than the live control — the same note the
#: auditor's own caps carry.
COUNTER_MAX_TOKENS = 300
LLM_TIMEOUT_SECONDS = 90.0

#: CONTRASTIVE CONJUNCTIONS — the words that make one sentence two claims.
#:
#: FOUND BY THIS LANE'S OWN SAMPLED AUDIT, over 60 real material claims from
#: the live top layer on 2026-09-25. Eight of the twenty sampled rows took a
#: deterministic negation and one of those eight was plainly wrong:
#:
#:   "Australia's high-level military posture remains unchanged, BUT the
#:    initial operational capability of new MQ-4C Triton UAVs ... adds a modest
#:    boost to its long-range surveillance."
#:
#: ``operational`` is on the ``closure`` group's negative side — it is there for
#: "the strait is operational" — so the claim read as a CHOKEPOINT statement at
#: sign -1 and the counter-query came back "triton uavs peregrine isr australia
#: high closed shut suspended". That query is about nothing. It is the same
#: polysemy R2 pruned ``holds`` / ``took`` / ``lost`` for, arriving through a
#: sentence rather than through a word.
#:
#: The guard is the cheapest one that catches the SHAPE rather than chasing the
#: vocabulary: a sentence carrying a contrastive conjunction is making two
#: claims, and negating one clause while recording a stance on the whole thing
#: is precisely the manufactured-disagreement failure R2 shipped zero pairs
#: rather than risk. It is NOT a loss of coverage — a declined claim goes to the
#: model leg, which exists for exactly the claims with no clean deterministic
#: negation, and arrives there behind the paraphrase gate.
#:
#: MEASURED COST, on the same 60 claims: the deterministic leg goes from 23 to
#: 15, and every one of the 8 it gives up is a compound sentence. The bad
#: Australia row is among them.
_CONTRASTIVE = frozenset({
    "but", "although", "though", "however", "whereas", "yet", "while",
    "despite", "nonetheless", "nevertheless", "conversely",
})

#: THE COUNTER HEADWORDS. For each (group, sign) the claim asserts, the words
#: that name the OPPOSITE state — the substance of the counter-query.
#:
#: Every entry is drawn from the opposite side of that group in
#: ``claim_contradiction._POLARITY_GROUPS`` and nothing else: this is an
#: ORDERING of an existing vocabulary, not a second vocabulary. The ordering is
#: the only thing added, and it is added because sorting a frozenset gives
#: "armistice calm" where a reporter writes "ceasefire truce" — the query has to
#: be one a search index has actually seen. ``test_counter_headwords_are_drawn
#: _from_the_r2_vocabulary`` pins the membership, so a future edit to the R2
#: table that drops a term turns this file red instead of silently emitting a
#: word the detector no longer knows.
#:
#: Keyed ``(group, claim_sign)`` — the sign the CLAIM takes; the value is what
#: to search for instead.
COUNTER_HEADWORDS: dict[tuple[str, int], tuple[str, ...]] = {
    ("closure", 1): ("reopened", "operating", "resumed"),
    ("closure", -1): ("closed", "shut", "suspended"),
    ("hostilities", 1): ("ceasefire", "truce", "withdrawal"),
    ("hostilities", -1): ("fighting", "strikes", "clashes"),
    ("supply", 1): ("restored", "uninterrupted", "surplus"),
    ("supply", -1): ("shortage", "outage", "rationing"),
    ("control", 1): ("retreated", "abandoned", "expelled"),
    ("control", -1): ("captured", "seized", "controls"),
    ("sanctions", 1): ("lifted", "eased", "waiver"),
    ("sanctions", -1): ("sanctioned", "banned", "embargo"),
}


@dataclass(frozen=True)
class CounterQuery:
    """One counter-query, and everything a row needs to say where it came from.

    ``query`` empty means NO QUERY WAS FORMULATED and ``reason`` says why. The
    caller must not search on an empty query — there is nothing to search for,
    and issuing the claim's own words would be the exact failure this module
    exists to prevent.
    """

    query: str
    source: str = QUERY_SOURCE_NONE
    polarity_group: str | None = None
    polarity_sign: int | None = None
    novel_tokens: int = 0
    reason: str = ""
    #: The counter headwords that were used, for the receipt's sampled rows.
    headwords: tuple[str, ...] = field(default_factory=tuple)

    @property
    def issued(self) -> bool:
        return bool(self.query)

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "query_source": self.source,
            "polarity_group": self.polarity_group,
            "polarity_sign": self.polarity_sign,
            "query_novel_tokens": self.novel_tokens,
            "reason": self.reason,
            "headwords": list(self.headwords),
        }


# ---------------------------------------------------------------------------
# The gate — is this query a paraphrase of the claim?
# ---------------------------------------------------------------------------


def novel_tokens(claim: str, query: str) -> int:
    """How many CONTENT tokens the query carries that the claim does not.

    Content means: not a stopword, not a negator, not a polarity term — i.e.
    exactly ``subject_tokens_of``'s answer, plus the polarity terms back, since
    the opposite state IS the substance of a counter-query and dropping it would
    make a perfect counter-query score zero.

    Zero is the definition of a paraphrase used everywhere in this lane: a query
    built only from the claim's own words cannot retrieve anything the claim's
    own query would not. It is a cheap test and it is the RIGHT cheap test —
    the expensive alternatives (embedding distance, an LLM judging its own
    output) either need a model on the gate, which defeats the point, or make
    the refusal unexplainable to the operator reading a row.
    """
    claim_tokens = set(tokenize(claim))
    return sum(
        1 for tok in dict.fromkeys(tokenize(query))
        if len(tok) > 2 and tok not in claim_tokens
    )


def is_paraphrase(claim: str, query: str) -> bool:
    """``True`` when the query adds nothing to the claim. See :func:`novel_tokens`."""
    return novel_tokens(claim, query) <= 0


# ---------------------------------------------------------------------------
# The deterministic leg
# ---------------------------------------------------------------------------


def _ordered_subject(claim: str) -> list[str]:
    """The claim's identity tokens, PROPER NOUNS FIRST, then first-appearance.

    Proper nouns lead because they are what makes a query about one crossing
    rather than about crossings: "Hormuz" retrieves the Strait, "strait"
    retrieves a category. The remainder follows in the order the claim used it,
    so the query reads like the sentence it came from and two runs over the same
    claim produce the same string forever.
    """
    tokens = tokenize(claim)
    subject = subject_tokens_of(claim)
    proper = proper_tokens_of(claim) & subject
    ordered: list[str] = []
    for tok in tokens:
        if tok in proper and tok not in ordered:
            ordered.append(tok)
    for tok in tokens:
        if tok in subject and tok not in ordered:
            ordered.append(tok)
    return ordered[:MAX_QUERY_SUBJECT_TOKENS]


def counter_query_from_polarity(claim: str) -> CounterQuery | None:
    """The claim's negation in the R2 vocabulary, or ``None`` when it takes no
    single side there.

    ``None`` in FOUR cases, and each is a deliberate refusal rather than a gap:

      * the claim takes no side at all — most published claims, by design; the
        vocabulary is narrow and widening it is how R2's first sweep produced 57
        false pairs. The model leg handles these.
      * the claim takes sides on MORE THAN ONE group. A compound sentence has no
        single negation, and picking one of its groups would counter half a
        claim while reporting a stance on all of it.
      * the claim carries a CONTRASTIVE CONJUNCTION (:data:`_CONTRASTIVE`) — one
        sentence making two claims. See that constant's note for the live row
        that put it here.
      * the claim is longer than :data:`MAX_CLAIM_CHARS`. A 400-character
        sentence enumerating five domains is not one proposition; R2 skips it
        for the same reason and this pass never selects it (see the handler's
        own selection note).
    """
    text = (claim or "").strip()
    if not text or len(text) > MAX_CLAIM_CHARS:
        return None
    tokens = tokenize(text)
    if any(tok in _CONTRASTIVE for tok in tokens):
        return None
    groups = polarity_of(text)
    if len(groups) != 1:
        return None
    group, sign = next(iter(groups.items()))
    headwords = COUNTER_HEADWORDS.get((group, int(sign)))
    if not headwords:
        return None
    subject = _ordered_subject(text)
    if not subject:
        # A polarity term with nothing to attach it to. "Reopened operating
        # resumed" is not a query about anything.
        return CounterQuery(
            query="", source=QUERY_SOURCE_NONE, polarity_group=group,
            polarity_sign=int(sign), reason=REASON_NO_SUBJECT,
        )
    query = " ".join([*subject, *headwords])[:MAX_QUERY_CHARS]
    return CounterQuery(
        query=query,
        source=QUERY_SOURCE_POLARITY,
        polarity_group=group,
        polarity_sign=int(sign),
        novel_tokens=novel_tokens(text, query),
        headwords=tuple(headwords),
    )


# ---------------------------------------------------------------------------
# The model leg
# ---------------------------------------------------------------------------

COUNTER_SYSTEM = (
    "You write SEARCH QUERIES for a contrary-evidence pass. You are given ONE "
    "claim this system published. Your ONLY job is to write the search query "
    "that would find the STRONGEST PUBLIC EVIDENCE AGAINST it.\n"
    "\n"
    "YOU MAY NOT ASSERT ANY FACT AND YOU MAY NOT RENDER A VERDICT. You are not "
    "being asked whether the claim is true, how likely it is, or what the "
    "evidence shows. Nothing you write will be read as a statement about the "
    "world — it will be typed into a search engine. A reply that argues, "
    "hedges, explains or concludes is discarded unread.\n"
    "\n"
    "WHAT THE OPPOSING PROPOSITION IS. Negate what the claim ASSERTS, not the "
    "words it uses. If the claim says a crossing is closed, the opposing "
    "proposition is that it is open — search for that. If the claim says an "
    "official resigned, the opposing proposition is that they remain in post. "
    "If the claim reports a figure, the opposing proposition is a different "
    "published figure for the same thing on the same date.\n"
    "\n"
    "A QUERY THAT RESTATES THE CLAIM IS THE ONE FAILURE THIS STEP HAS, and it "
    "is rejected in code before any search runs. Your query MUST carry at "
    "least one substantive word the claim does not, and that word is normally "
    "the opposite state itself.\n"
    "\n"
    "KEEP THE SUBJECT. Name the same actors, places and dates, so the results "
    "are about the same thing rather than about the same topic. Short, "
    "specific, keyword-shaped. No boolean syntax, no quotation marks, no site: "
    "operators.\n"
    "\n"
    "Respond with STRICT JSON and nothing else — no prose, no code fences:\n"
    '{"counter_query": "<the search query>"}\n'
    'Return {"counter_query": ""} when the claim has no checkable opposite — a '
    "definition, a statement about this system's own coverage, a pure "
    "judgement of intent. An empty answer is legitimate and useful; a query "
    "invented to fill the slot is not."
)


def counter_prompt(claim: str, *, desk_key: str = "", as_of: str = "") -> str:
    lines = [f"CLAIM: {claim}"]
    if desk_key:
        lines.append(f"DESK: {desk_key}")
    if as_of:
        lines.append(f"PUBLISHED: {as_of}")
    return "\n".join(lines)


def validate_model_query(claim: str, content: str) -> CounterQuery:
    """Parse and RE-VALIDATE the model's reply. Never trusts the prompt.

    Four refusals, all in code:

      * an unparsable reply, or one with no ``counter_query`` key ⇒
        ``counter_query_model_failed``;
      * an empty query — the model's own legitimate "this claim has no checkable
        opposite" ⇒ ``counter_query_model_declined``. That is a DIFFERENT row
        from a failure and it is not a defect;
      * a query that adds no content token the claim does not already carry ⇒
        ``counter_query_paraphrase``, refused before any search is spent;
      * anything past :data:`MAX_QUERY_CHARS` is truncated, then re-gated.

    ``normalize_core_plane_text`` runs first for the reason every core-plane
    reader on this platform runs it: these models emit full-width 【N】 brackets
    and other wide punctuation, and a query carrying them retrieves nothing for
    a reason that has nothing to do with the world.
    """
    parsed = parse_strict_json_object(content or "")
    if parsed is None or "counter_query" not in parsed:
        return CounterQuery(query="", reason=REASON_MODEL_FAILED)
    raw = normalize_core_plane_text(str(parsed.get("counter_query") or "")).strip()
    query = raw[:MAX_QUERY_CHARS].strip()
    if not query:
        return CounterQuery(query="", reason=REASON_MODEL_EMPTY)
    novel = novel_tokens(claim, query)
    if novel <= 0:
        logger.info(
            "contrary_evidence.counter_query_paraphrase claim=%r query=%r",
            claim[:120], query[:120],
        )
        return CounterQuery(
            query="", source=QUERY_SOURCE_MODEL, novel_tokens=0,
            reason=REASON_PARAPHRASE,
        )
    return CounterQuery(
        query=query, source=QUERY_SOURCE_MODEL, novel_tokens=novel,
    )


async def propose_counter_query(
    llm: Any, claim: str, *, desk_key: str = "", as_of: str = "",
    temperature: float = 1.0,
) -> CounterQuery:
    """ONE bounded core-plane call. Degrades to a reasoned refusal, never raises.

    ``temperature`` defaults to the house value (the standing rule: temperature
    1.0 everywhere on this platform, and the descriptor declares it). A failure
    of any kind — a timeout, a wedged plane, an unparsable reply — costs this
    claim its counter-query and nothing else: the row still lands, carrying
    ``search_failed`` and the reason, which is what makes a dead LLM plane
    visible in the ledger instead of looking like a quiet week for contrary
    evidence.
    """
    if llm is None:
        return CounterQuery(query="", reason=REASON_NO_LLM)
    try:
        response = await asyncio.wait_for(
            llm.chat_complete(
                [{"role": "user",
                  "content": counter_prompt(claim, desk_key=desk_key,
                                            as_of=as_of)}],
                max_tokens=COUNTER_MAX_TOKENS,
                temperature=temperature,
                system=COUNTER_SYSTEM,
            ),
            timeout=LLM_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return CounterQuery(query="", reason=REASON_MODEL_FAILED)
    except Exception as exc:  # degrade-not-break
        logger.warning("contrary_evidence.counter_query_failed err=%s", exc)
        return CounterQuery(query="", reason=REASON_MODEL_FAILED)
    return validate_model_query(claim, getattr(response, "content", "") or "")


def counter_headword_audit() -> list[tuple[str, int, str]]:
    """Every ``(group, claim_sign, headword)`` this module would emit.

    Exists for the membership test and for the report's own audit — a reader
    should be able to enumerate the entire deterministic vocabulary without
    reading the dict literal.
    """
    return [
        (group, sign, word)
        for (group, sign), words in sorted(COUNTER_HEADWORDS.items())
        for word in words
    ]


def headwords_are_in_vocabulary() -> list[str]:
    """Headwords that are NOT on the opposite side of their R2 group.

    Empty is the only acceptable answer. Returned rather than asserted so the
    test can name every offender in one failure instead of one per run.
    """
    bad: list[str] = []
    for group, claim_sign, word in counter_headword_audit():
        opposite: Sequence[str] = polarity_side_terms(group, -claim_sign)
        if word not in opposite:
            bad.append(f"{group}/{claim_sign:+d}: {word!r}")
    return bad


def describe(counter: CounterQuery, claim: Mapping[str, Any] | None = None) -> str:
    """One human line for the receipt's sampled rows."""
    if not counter.issued:
        return f"(no query — {counter.reason or 'unstated'})"
    group = counter.polarity_group or "-"
    return (
        f"[{counter.source}/{group}] {counter.query} "
        f"(+{counter.novel_tokens} novel)"
    )


def contrastive_terms() -> frozenset[str]:
    """The contrastive vocabulary, for a test and for the report's own audit."""
    return _CONTRASTIVE


__all__ = [
    "COUNTER_HEADWORDS",
    "COUNTER_MAX_TOKENS",
    "COUNTER_SYSTEM",
    "MAX_QUERY_CHARS",
    "MAX_QUERY_SUBJECT_TOKENS",
    "QUERY_SOURCE_MODEL",
    "QUERY_SOURCE_NONE",
    "QUERY_SOURCE_POLARITY",
    "REASON_MODEL_EMPTY",
    "REASON_MODEL_FAILED",
    "REASON_NO_LLM",
    "REASON_NO_SUBJECT",
    "REASON_PARAPHRASE",
    "CounterQuery",
    "contrastive_terms",
    "counter_headword_audit",
    "counter_prompt",
    "counter_query_from_polarity",
    "describe",
    "headwords_are_in_vocabulary",
    "is_paraphrase",
    "novel_tokens",
    "propose_counter_query",
    "validate_model_query",
]
