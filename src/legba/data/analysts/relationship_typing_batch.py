# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``relationship_typing_batch`` — BATCH form of the reifier's typing call (K-G2).

``relationship_reifier`` issues **one LLM call per candidate**. At the live cap
(40 candidates / 12 h) that is 80 calls/day and — measured 2026-08-03 —
≈12 typed edges/day against ≈9,941 candidate arrivals/day. This module is the
batched counterpart: **N candidates per call**, one shared instruction block,
per-candidate evidence snippets, and a structured array response carrying one
verdict per candidate.

Nothing here reinvents the reifier's judgement. The batch prompt restates the
SAME task, the SAME closed ``rel_type`` vocabulary
(:data:`~.relationship_reifier.ALLOWED_REL_TYPES`), the SAME intent/channel sets
and the SAME intermediary SELECT-or-null discipline; every verdict is validated
through the reifier's own :func:`~.relationship_reifier._coerce_typing`, so a
batched verdict is accepted on exactly the terms a single-candidate verdict is.
The batch layer adds three things and only three:

  1. **Correlation.** Each candidate carries a small integer ``idx``; the model
     must echo it. Verdicts are matched by ``idx``, never by position — a model
     that reorders, drops or duplicates entries is detected rather than
     silently mis-assigned to the wrong pair.
  2. **Parse integrity accounting.** :class:`BatchParseResult` reports what was
     recovered vs. what the batch asked for (``missing`` / ``unexpected`` /
     ``duplicate`` idx), so the safe batch size N can be MEASURED against real
     model behaviour instead of assumed.
  3. **Salvage.** A truncated array (the dominant failure at large N — the
     model runs out of completion budget mid-object) still yields every
     complete object that preceded the truncation, so a partly-spent call is
     not a total loss. This mirrors the reifier's degrade-not-drop rule at the
     batch level: one bad entry costs one candidate, never the batch.

Discipline notes:

  * PURE. No I/O, no LLM handle, no pg. Callers own transport (the bake-off
    runner in ``scripts/kg2_typing_bakeoff.py``; a future reifier batch path).
  * The rationale field is one line and exists for the human worksheet — it is
    NOT consulted by validation and never reaches a ``NexusPayload``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..provenance.models import NexusPayload
from ._tradecraft import with_preamble
from .relationship_reifier import (
    ALLOWED_REL_TYPES,
    MAX_FACTS_CONTEXT,
    _coerce_typing,
)

logger = logging.getLogger(__name__)

HARNESS_VERSION: str = "0.1.0"

DEFAULT_BATCH_SIZE: int = 12
"""Candidates per call. Measured default — see docs/TYPING_BAKEOFF_2026-08-03.md.
Not a guess: the bake-off sweeps N and reports parse integrity per N per model."""

DEFAULT_EVIDENCE_CHARS: int = 420
"""Per-candidate evidence budget inside a batch. The single-candidate prompt
allows 1,200 chars because it pays that once; a batch pays it N times, so the
snippet is tightened. 420 chars ≈ the lead sentence or two of a co-mention
excerpt, which is where the relationship actually appears."""

DEFAULT_MAX_TOKENS_PER_VERDICT: int = 280
"""Completion budget to reserve per candidate. MEASURED, not estimated: the
first live batch (core 120B, N=12) spent 3,102 completion tokens on 12 verdicts
— **258 tokens/verdict**. A verdict's *content* is only ≈90 tokens; the rest is
pretty-printing, because every model in the roster emits indented JSON however
the prompt is worded. Budgeting for the compact form is what truncates large
batches, so the reservation tracks the observed indented form with headroom."""

MAX_TOKENS_BATCH_OVERHEAD: int = 96
"""Fixed completion allowance on top of N × per-verdict — array punctuation,
a stray leading newline, and (on reasoning models that leak a preamble) a short
run-up before the array opens."""


def max_tokens_for_batch(
    n: int,
    *,
    per_verdict: int = DEFAULT_MAX_TOKENS_PER_VERDICT,
    overhead: int = MAX_TOKENS_BATCH_OVERHEAD,
) -> int:
    """Completion budget for a batch of ``n`` candidates.

    Linear in N by design: truncation is the batch failure mode that costs the
    most candidates per wasted token, so the budget must scale with the batch
    rather than sit at the single-call default."""
    return max(1, int(n)) * int(per_verdict) + int(overhead)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

#: The batch system prompt. Deliberately a restatement of the reifier's
#: single-candidate contract (same fields, same rules, same worked examples) with
#: the array/idx protocol layered on top — so a model's batched judgement is
#: comparable to its unbatched judgement and neither prompt is the confound.
BATCH_SYSTEM_PROMPT = with_preamble(
    """TASK — you are typing MANY candidate entity relationships in one pass. Each numbered candidate gives two co-mentioned entities and the evidence they were co-mentioned in. For EACH candidate independently, decide whether a meaningful, directed relationship holds and, if so, classify it.

Return ONE JSON array, nothing else. Exactly one object per candidate, in the order given:
[
  {
    "idx": <the candidate's number, copied exactly>,
    "same_entity": true|false,      // true => A and B are two NAMES for ONE entity
    "related": true|false,          // false => merely co-mentioned, no real relationship
    "subject": "<acting entity>",   // who initiates/conducts
    "object": "<affected entity>",  // who is targeted/affected
    "intermediary": "<proxy>"|null, // a cut-out the relationship runs through, else null
    "rel_type": "<one of the allowed types>",
    "intent": "supportive"|"hostile"|"dual-use"|"neutral",
    "channel": "direct"|"proxy"|"covert"|"institutional",
    "confidence": 0.0-1.0,
    "rationale": "<one short line, max 15 words>"
  },
  ...
]

Rules:
- SAME-ENTITY CHECK FIRST. If A and B are two names for the SAME thing — an acronym and its expansion (IRGC / Revolutionary Guards Corps), an abbreviation, a former name, a translation, a nickname — set "same_entity": true and "related": false, and stop there for that candidate. An entity is not related to itself. This is NOT the same as a part-whole or membership relation: a subsidiary, a subcommittee, a province or a member state is a DIFFERENT entity from its parent, so those get same_entity=false and a normal rel_type.
- Emit an object for EVERY candidate, including the ones you reject. Never skip a number, never merge two candidates, never invent a number that was not asked for.
- Judge each candidate ONLY on its own evidence. Candidates in the same batch are unrelated to each other; do not let one candidate's framing colour another's.
- Pick rel_type ONLY from the allowed list given below. If merely co-mentioned with no real relationship, set related=false (rel_type may be omitted or null).
- Be sparse. A shared dateline, a list of countries in one article, a sports fixture, or two names appearing in the same roundup is NOT a relationship. Reject freely — a wrong edge is more expensive than a missing one.
- INTERMEDIARY rule: set "intermediary" to null UNLESS that candidate offers a "Candidate intermediaries" list AND one of those listed entities genuinely acts as the cut-out the A->B relationship runs through. Copy it VERBATIM from that candidate's own offered list — never name a proxy that is not on the list, however plausible, and never borrow one from another candidate.
- Keep "rationale" to one short line. It is read by a human reviewer, not parsed.

Worked examples (single candidates, shown for calibration):
  - Hostile supply via a proxy: A arms a militia that attacks B -> subject=A, object=B, intermediary=the militia, rel_type=SuppliesWeaponsTo, channel=proxy, intent=hostile.
  - Institutional membership: country X joins alliance Y -> subject=X, object=Y, intermediary=null, rel_type=MemberOf, channel=institutional, intent=supportive.
  - Dual-use presence: company C operates a facility in country D with no stated alignment -> subject=C, object=D, intermediary=null, rel_type=OperatesIn, channel=direct, intent=dual-use.
  - Bare co-mention: two countries listed in the same weather roundup -> related=false.
  - Same entity twice: IRGC and Revolutionary Guards Corps -> same_entity=true, related=false."""
)


@dataclass
class BatchCandidate:
    """One candidate inside a batch.

    ``idx`` is the correlation key the model must echo. ``source``/``target`` are
    the ALREADY-canonicalised endpoint surfaces (the reifier canonicalises and
    drops junk/self-loops before spending an LLM call; the batch path inherits
    that pre-filter rather than re-implementing it).
    """

    idx: int
    source: str
    target: str
    evidence_text: str = ""
    facts: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    intermediaries: Sequence[str] = field(default_factory=tuple)
    #: Free-form carrier for the caller's own row identity (a proposed_edges
    #: uuid, a worksheet row number). Never rendered into the prompt.
    ref: Any = None


def build_batch_user_prompt(
    candidates: Sequence[BatchCandidate],
    *,
    evidence_chars: int = DEFAULT_EVIDENCE_CHARS,
    max_facts: int = MAX_FACTS_CONTEXT,
) -> str:
    """Render N candidates into one user prompt.

    The allowed-type vocabulary is stated ONCE for the whole batch (this is
    where batching wins on prompt tokens — the single-call path repeats the full
    list, plus the whole system preamble, for every candidate)."""
    lines: list[str] = [
        f"Allowed rel_type values: {', '.join(ALLOWED_REL_TYPES)}",
        "",
        f"There are {len(candidates)} candidates. Return a JSON array of "
        f"{len(candidates)} objects, one per candidate, each echoing its idx.",
        "",
    ]
    for cand in candidates:
        lines.append(f"--- CANDIDATE {cand.idx} ---")
        lines.append(f"Entity A: {cand.source}")
        lines.append(f"Entity B: {cand.target}")
        evidence = str(cand.evidence_text or "").strip()
        lines.append(f"Evidence: {evidence[:evidence_chars] if evidence else '(none)'}")
        if cand.facts:
            lines.append("Recent facts:")
            for f in list(cand.facts)[:max_facts]:
                lines.append(
                    f"  - {f.get('subject')} {f.get('predicate')} {f.get('value')}"
                )
        if cand.intermediaries:
            lines.append(
                "Candidate intermediaries (select ONE verbatim only if it is "
                "the cut-out this A->B relationship runs through, else null):"
            )
            for c in cand.intermediaries:
                lines.append(f"  - {c}")
        lines.append("")
    lines.append(
        f"Now return the JSON array of {len(candidates)} verdict objects, "
        "nothing else."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _scan_top_level_objects(text: str, start: int) -> tuple[list[dict[str, Any]], bool]:
    """Walk ``text`` from ``start`` collecting balanced top-level ``{...}`` blocks.

    String-aware (a brace inside a quoted value must not move the depth counter)
    and escape-aware. Returns ``(objects, truncated)`` where ``truncated`` is
    True when the scan ended inside an unterminated object — the signature of a
    completion-budget cut-off, which is exactly the failure this salvages."""
    objects: list[dict[str, Any]] = []
    depth = 0
    obj_start = -1
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and obj_start >= 0:
                    chunk = text[obj_start : i + 1]
                    try:
                        parsed = json.loads(chunk)
                    except (json.JSONDecodeError, ValueError):
                        parsed = None
                    if isinstance(parsed, dict):
                        objects.append(parsed)
                    obj_start = -1
    return objects, depth > 0


def extract_batch_objects(raw: str) -> tuple[list[dict[str, Any]], bool]:
    """Pull the per-candidate verdict objects out of a raw batch response.

    Tolerant by design — the point of the bake-off is to compare MODELS, so the
    parser must not manufacture a parse-failure that is really a formatting
    quirk (a ```json fence, a "Here are the verdicts:" preamble, a reasoning
    model's leading commentary). Order of attack:

      1. strict ``json.loads`` of the whole response (the well-behaved case);
      2. strict load of the first balanced ``[...]`` slice;
      3. brace-scan salvage from the first ``{`` — recovers every complete
         object even when the array was never closed (truncation).

    Returns ``(objects, truncated)``."""
    text = str(raw or "").strip()
    if not text:
        return [], False
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        whole = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        whole = None
    if isinstance(whole, list):
        return [o for o in whole if isinstance(o, dict)], False
    if isinstance(whole, dict):
        # A model that wrapped the array, e.g. {"verdicts": [...]}.
        for value in whole.values():
            if isinstance(value, list) and any(isinstance(o, dict) for o in value):
                return [o for o in value if isinstance(o, dict)], False
        return [whole], False

    lb = text.find("[")
    if lb >= 0:
        depth = 0
        in_string = False
        escaped = False
        for i in range(lb, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        arr = json.loads(text[lb : i + 1])
                    except (json.JSONDecodeError, ValueError):
                        break
                    if isinstance(arr, list):
                        return [o for o in arr if isinstance(o, dict)], False
                    break

    first_brace = text.find("{")
    if first_brace < 0:
        return [], False
    return _scan_top_level_objects(text, first_brace)


def _as_confidence(value: Any) -> float | None:
    """A model-supplied 0..1 confidence, clamped, or ``None`` when unusable."""
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


@dataclass
class BatchVerdict:
    """One candidate's decision, carried alongside its correlation key."""

    idx: int
    ref: Any
    source: str
    target: str
    #: True when the model asserted a relationship AND the reifier's own
    #: coercion accepted it. False is a REJECT — either the model said
    #: related=false or the verdict failed validation (off-list rel_type, junk
    #: endpoint, self-loop after canonicalisation, ...).
    accepted: bool
    rel_type: str | None = None
    polarity: int | None = None
    intent: str | None = None
    channel: str | None = None
    intermediary: str | None = None
    confidence: float | None = None
    rationale: str = ""
    #: Why a non-accepted verdict was not accepted: ``model_reject`` (the model
    #: chose related=false), ``coercion_reject`` (validation refused it), or
    #: ``alias_pair`` (the two surfaces name ONE entity — see ``same_entity``).
    reject_reason: str | None = None
    #: The typer judged A and B to be two names for the same entity. NEVER an
    #: edge — an entity is not related to itself — and never a plain rejection
    #: either: it is a merge-candidate signal the caller routes. See
    #: :mod:`.reifier_alias_pairs` for why this class needs its own answer
    #: rather than being caught by the canon or the keeper gate.
    same_entity: bool = False
    payload: NexusPayload | None = None


@dataclass
class BatchParseResult:
    """Everything the caller needs to score one batch call."""

    verdicts: list[BatchVerdict] = field(default_factory=list)
    #: idx values the batch asked about that came back with no usable object.
    missing_idx: list[int] = field(default_factory=list)
    #: idx values returned that were never asked for (hallucinated correlation).
    unexpected_idx: list[int] = field(default_factory=list)
    #: idx values returned more than once (first wins).
    duplicate_idx: list[int] = field(default_factory=list)
    #: The response ended inside an unterminated object.
    truncated: bool = False
    #: Objects recovered from the raw text, before idx matching.
    raw_object_count: int = 0

    @property
    def requested(self) -> int:
        return len(self.verdicts) + len(self.missing_idx)

    @property
    def recovered(self) -> int:
        return len(self.verdicts)

    @property
    def parse_ok(self) -> bool:
        """A clean batch: every requested idx answered, nothing spurious."""
        return not (
            self.missing_idx
            or self.unexpected_idx
            or self.duplicate_idx
            or self.truncated
        )

    @property
    def recovery_rate(self) -> float:
        req = self.requested
        return (self.recovered / req) if req else 0.0


def parse_batch_response(
    raw: str,
    candidates: Sequence[BatchCandidate],
    *,
    sports_gate_text: Mapping[int, str] | None = None,
) -> BatchParseResult:
    """Turn one raw batch response into per-candidate verdicts.

    Every asserted relationship is pushed through the reifier's
    :func:`_coerce_typing`, so batching cannot loosen what production accepts:
    the closed rel_type list, junk/demonym/self-loop endpoint drops, the
    SELECT-or-null intermediary rule, the deterministic intent->polarity map and
    the D14 sports gate all still apply.

    ``sports_gate_text`` optionally supplies the WIDER gate text per idx (the
    excerpt unioned with the backing signals' title+summary, as
    ``relationship_reifier._sports_gate_text`` builds it). Absent, the
    candidate's own evidence is used.
    """
    by_idx = {c.idx: c for c in candidates}
    objects, truncated = extract_batch_objects(raw)

    result = BatchParseResult(truncated=truncated, raw_object_count=len(objects))
    seen: dict[int, dict[str, Any]] = {}
    positional: list[dict[str, Any]] = []

    for obj in objects:
        rawidx = obj.get("idx", obj.get("index", obj.get("id")))
        try:
            idx = int(rawidx)
        except (TypeError, ValueError):
            positional.append(obj)
            continue
        if idx not in by_idx:
            result.unexpected_idx.append(idx)
            continue
        if idx in seen:
            result.duplicate_idx.append(idx)
            continue
        seen[idx] = obj

    # A model that emitted well-formed objects but no usable idx at all is
    # recoverable ONLY when it returned exactly as many objects as were asked
    # for and none carried an idx — then position is the correlation. Any other
    # shape is left as missing rather than guessed: mis-assigning a verdict to
    # the wrong pair is the one error worse than losing it.
    if positional and not seen and len(positional) == len(candidates):
        for cand, obj in zip(candidates, positional):
            seen[cand.idx] = obj

    for cand in candidates:
        obj = seen.get(cand.idx)
        if obj is None:
            result.missing_idx.append(cand.idx)
            continue
        rationale = str(obj.get("rationale") or "").strip()[:300]
        gate_text = (
            (sports_gate_text or {}).get(cand.idx) or cand.evidence_text or ""
        )
        # SAME-ENTITY FIRST, and before ``related`` — a model may answer both
        # (an acronym IS "related" to its expansion in every ordinary sense).
        # An identity claim is never an edge, so it short-circuits coercion
        # entirely rather than being coerced and then discarded.
        if obj.get("same_entity", False):
            result.verdicts.append(
                BatchVerdict(
                    idx=cand.idx,
                    ref=cand.ref,
                    source=cand.source,
                    target=cand.target,
                    accepted=False,
                    rationale=rationale,
                    reject_reason="alias_pair",
                    same_entity=True,
                    confidence=_as_confidence(obj.get("confidence")),
                )
            )
            continue
        if not obj.get("related", False):
            result.verdicts.append(
                BatchVerdict(
                    idx=cand.idx,
                    ref=cand.ref,
                    source=cand.source,
                    target=cand.target,
                    accepted=False,
                    rationale=rationale,
                    reject_reason="model_reject",
                )
            )
            continue
        payload = _coerce_typing(
            obj,
            fallback_subject=cand.source,
            fallback_object=cand.target,
            allowed_intermediaries=tuple(cand.intermediaries),
            evidence_text=gate_text,
        )
        if payload is None:
            result.verdicts.append(
                BatchVerdict(
                    idx=cand.idx,
                    ref=cand.ref,
                    source=cand.source,
                    target=cand.target,
                    accepted=False,
                    rationale=rationale,
                    reject_reason="coercion_reject",
                )
            )
            continue
        result.verdicts.append(
            BatchVerdict(
                idx=cand.idx,
                ref=cand.ref,
                source=cand.source,
                target=cand.target,
                accepted=True,
                rel_type=payload.rel_type,
                polarity=payload.polarity,
                intent=payload.intent,
                channel=payload.channel,
                intermediary=payload.intermediary,
                confidence=payload.confidence,
                rationale=rationale,
                payload=payload,
            )
        )
    return result


__all__ = [
    "HARNESS_VERSION",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_EVIDENCE_CHARS",
    "BATCH_SYSTEM_PROMPT",
    "BatchCandidate",
    "BatchVerdict",
    "BatchParseResult",
    "build_batch_user_prompt",
    "extract_batch_objects",
    "parse_batch_response",
    "max_tokens_for_batch",
]
