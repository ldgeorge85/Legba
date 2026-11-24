# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The MODEL step of the drift assessment (lane D-2) — behind
``LEGBA_DRIFT_ASSESS_MODEL``, OFF by default, on the free core plane only.

THE MODEL DOES NOT TYPE. Code alone sets the change type (and the components
of ``mixed``). The model step does exactly two things:

1. the one- or two-sentence SUMMARY a desk prompt carries;
2. the claim-effect answer for a NOT-CHECKABLE citer (a cited span that
   never appeared verbatim in the page), stored beside the code result.

It may additionally return ONE optional field, ``code_type_doubt``: a single
type from the closed list it believes the code got wrong, with a quoted
before/after pair as evidence. The quotes are verified against the stored
texts; a verified doubt is stored BESIDE the code type, never over it, and
lifts the row in review order. An unverifiable doubt is dropped and counted.

It reads ONLY the bounded diff (headline before/after, changed sentence
pairs, removed and added sentences, each clipped) plus the code's type. It is
never asked why a change was made: the schema (:data:`ASSESS_OUTPUT_KEYS`)
has no motive field, the prompt forbids motive, and a summary carrying motive
language is refused. A reply that does not parse or validate is recorded
loudly (``model_status = parse_failed`` with the reason); the code assessment
and the code summary then stand — never a guess.

Calls go through the injected handler (``chat_complete``) at
:data:`legba.shared.sampling.HOUSE_TEMPERATURE`, with no ``max_tokens``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional

from legba.shared.sampling import HOUSE_TEMPERATURE

from .evidence_drift_assess import (
    ASSESS_RULES_VERSION,
    COMPONENT_TYPES,
    SUMMARY_MAX_CHARS,
    Assessment,
    Diff,
    _clip,
)
from .provenance.text_fold import normalize_for_match

logger = logging.getLogger(__name__)

__all__ = [
    "ASSESS_OUTPUT_KEYS",
    "ASSESS_PROMPT_VERSION",
    "ASSESS_SYSTEM",
    "DOUBT_KEYS",
    "EFFECT_OUTPUT_KEYS",
    "MODEL_TIMEOUT_S",
    "ModelResult",
    "assess_prompt",
    "claim_effect_prompt",
    "combine",
    "parse_assessment",
    "parse_claim_effect",
    "run_model",
    "stamp",
]

#: Bumped when the prompt or the output schema changes.
ASSESS_PROMPT_VERSION = "drift-assess-prompt/2"
MODEL_TIMEOUT_S = 180.0

#: Diff bounds handed to the model.
PROMPT_MAX_PAIRS = 10
PROMPT_MAX_REMOVED = 10
PROMPT_MAX_ADDED = 12
PROMPT_SIDE_CHARS = 300

#: The ONLY keys the assessment reply may carry. No motive, no intent, no
#: reason-for-the-change field exists — a reply with any other key fails.
ASSESS_OUTPUT_KEYS = frozenset({"summary", "code_type_doubt"})
DOUBT_KEYS = frozenset({"type", "before", "after"})
EFFECT_OUTPUT_KEYS = frozenset({"effect", "after_quote"})
MODEL_EFFECTS = ("survives", "changed", "gone", "cannot_tell")

#: Motive / intent language a summary may not carry.
_MOTIVE = re.compile(
    r"\b(deliberate(ly)?|intentional(ly)?|to (hide|conceal|cover|mislead|deceive|bury|whitewash)"
    r"|cover(ed|ing)?[- ]up|in order to|so as to|propaganda|agenda|motive|motivated|sought to|tried to hide"
    r"|quietly|stealth(ily)?|sneak(y|ily)?|secretly|scrub(bed)?|whitewash(ed)?|spin)\b", re.I)

ASSESS_SYSTEM = (
    "You compare two versions of one news page and describe WHAT changed, never why. "
    "You are given only the changed parts. Answer with one JSON object and nothing else."
)

_TYPES_HELP = """The type list (code chose one; you may doubt it):
spelling_or_name_correction, update_new_facts, clarification, attribution_change,
reframing (headline or tone, same facts), claim_removed, claim_reversed (a statement negated),
fact_corrected (a value - a figure, place, person, charge - corrected to a different value),
retraction, page_gone, content_replaced (the address now carries other content)."""

_SCHEMA_HELP = """Reply with exactly this JSON shape:
{"summary": "<one or two plain sentences, at most 40 words, saying what changed, e.g. 'death toll revised 19 -> 17; casualty list added'>",
 "code_type_doubt": null}
or, ONLY if you are confident the code's type misses or misnames the most important change:
{"summary": "...",
 "code_type_doubt": {"type": "<ONE type from the list>", "before": "<earlier text, quoted exactly>", "after": "<later text, quoted exactly, or null>"}}
Rules: quote text exactly as given. Do not say or suggest why the source made the change, what it intended, or whether it was honest; describe only what the text shows. No other keys."""


def _side(text: Optional[str]) -> Optional[str]:
    return _clip(text, PROMPT_SIDE_CHARS)


def _diff_block(diff: Diff, *, earlier_title: Optional[str] = None,
                later_title: Optional[str] = None) -> str:
    """The bounded diff, rendered for the model."""
    lines: list[str] = []
    if earlier_title or later_title:
        lines.append(f"HEADLINE before: {_side(earlier_title) or '(none)'}")
        lines.append(f"HEADLINE after:  {_side(later_title) or '(none)'}")
    if diff.pairs:
        lines.append("CHANGED SENTENCES (before => after):")
        for b, a in diff.pairs[:PROMPT_MAX_PAIRS]:
            lines.append(f"- {_side(b)}\n  => {_side(a)}")
        if len(diff.pairs) > PROMPT_MAX_PAIRS:
            lines.append(f"  ({len(diff.pairs) - PROMPT_MAX_PAIRS} more changed sentences not shown)")
    if diff.removed:
        lines.append("REMOVED (in the earlier version only):")
        lines.extend(f"- {_side(s)}" for s in diff.removed[:PROMPT_MAX_REMOVED])
        if len(diff.removed) > PROMPT_MAX_REMOVED:
            lines.append(f"  ({len(diff.removed) - PROMPT_MAX_REMOVED} more removed not shown)")
    if diff.added:
        lines.append("ADDED (in the later version only):")
        lines.extend(f"- {_side(s)}" for s in diff.added[:PROMPT_MAX_ADDED])
        if len(diff.added) > PROMPT_MAX_ADDED:
            lines.append(f"  ({len(diff.added) - PROMPT_MAX_ADDED} more added not shown)")
    if not (diff.pairs or diff.removed or diff.added):
        lines.append("(the body text did not change)")
    return "\n".join(lines)


def assess_prompt(diff: Diff, *, code_type: str, code_components: Optional[list[str]] = None,
                  earlier_title: Optional[str] = None, later_title: Optional[str] = None) -> str:
    """The summary request: the bounded diff, the code's type, the schema."""
    kind = code_type if code_type != "mixed" else "mixed: " + ", ".join(code_components or [])
    return (_diff_block(diff, earlier_title=earlier_title, later_title=later_title)
            + f"\n\nCODE'S TYPE FOR THIS CHANGE: {kind}\n\n" + _TYPES_HELP + "\n\n" + _SCHEMA_HELP)


def claim_effect_prompt(span: str, diff: Diff) -> str:
    """For a citation whose text never appeared verbatim in the page: does the
    claim it carries still stand in the later version?"""
    body = _diff_block(diff)
    return (
        f"A report cited this text from the page:\n\"{_side(span)}\"\n\n"
        f"The page then changed. The changed parts:\n{body}\n\n"
        "Does the claim in the cited text still stand in the later version? Reply with exactly "
        '{"effect": "survives"|"changed"|"gone"|"cannot_tell", "after_quote": "<the later sentence that '
        'now carries the claim, quoted exactly, or null>"}. Describe only what the text shows; do not say why '
        "the source changed it. No other keys."
    )


class ModelResult:
    """The model step's outcome for one change (or one citer)."""

    def __init__(self, status: str, data: Optional[dict[str, Any]] = None,
                 reason: Optional[str] = None, raw: Optional[str] = None) -> None:
        self.status = status          # ok | parse_failed | call_failed
        self.data = data or {}
        self.reason = reason
        self.raw = raw

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "data": self.data, "reason": self.reason,
                "raw": None if self.raw is None else self.raw[:2000]}


def _strip(content: str) -> str:
    """The JSON object in a reply: drop think blocks, fences, full-width
    brackets; take the outermost {...}."""
    c = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S)
    c = c.replace("【", "[").replace("】", "]")
    c = re.sub(r"^```(?:json)?|```$", "", c.strip(), flags=re.M).strip()
    start, end = c.find("{"), c.rfind("}")
    return c[start:end + 1] if start >= 0 and end > start else c


def _in_text(quote: Optional[str], text: str) -> bool:
    if not quote:
        return False
    q = normalize_for_match(str(quote)).rstrip(".…").strip()
    return len(q) >= 12 and q in normalize_for_match(text)


_QUOTED = re.compile(r"['\"‘“]([^'\"‘’“”]{2,200})['\"’”]")


def _unquote_page_text(summary: str, diff: Diff) -> str:
    """The summary with every quoted phrase that IS page text removed: a
    summary that quotes the changed words ("'deliberate attempt' ->
    'attempt'") describes the text, it does not state a motive."""
    texts = normalize_for_match(diff.earlier_text + "\n" + diff.later_text)

    def _drop(m: re.Match[str]) -> str:
        q = normalize_for_match(m.group(1)).strip()
        return " " if q and q in texts else m.group(0)

    return _QUOTED.sub(_drop, summary)


def parse_assessment(content: str, diff: Diff) -> ModelResult:
    """Validate one summary reply. Unknown keys (a motive field among them), a
    missing summary or one carrying motive language are refused
    (``parse_failed`` with the reason). A ``code_type_doubt`` is kept only
    when its type is in the closed list and its quotes are found in the
    texts; otherwise it is dropped and the reason recorded."""
    try:
        obj = json.loads(_strip(content))
    except (ValueError, TypeError) as exc:
        return ModelResult("parse_failed", reason=f"not_json: {type(exc).__name__}", raw=content)
    if not isinstance(obj, dict):
        return ModelResult("parse_failed", reason="not_an_object", raw=content)
    extra = set(obj) - ASSESS_OUTPUT_KEYS
    if extra:
        return ModelResult("parse_failed", reason=f"unexpected_keys: {sorted(extra)}", raw=content)
    summary = obj.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return ModelResult("parse_failed", reason="summary_missing", raw=content)
    summary = " ".join(summary.split())
    if _MOTIVE.search(_unquote_page_text(summary, diff)):
        return ModelResult("parse_failed", reason="summary_states_motive", raw=content)
    doubt, dropped = None, None
    raw_doubt = obj.get("code_type_doubt")
    if raw_doubt is not None:
        if not isinstance(raw_doubt, dict) or set(raw_doubt) - DOUBT_KEYS:
            dropped = "doubt_shape"
        elif raw_doubt.get("type") not in COMPONENT_TYPES:
            dropped = f"doubt_type_not_in_list: {raw_doubt.get('type')!r}"
        elif not _in_text(raw_doubt.get("before"), diff.earlier_text):
            dropped = "doubt_before_not_in_earlier_text"
        elif raw_doubt.get("after") and not _in_text(raw_doubt.get("after"), diff.later_text):
            dropped = "doubt_after_not_in_later_text"
        else:
            doubt = {"type": raw_doubt["type"], "before": _clip(raw_doubt.get("before")),
                     "after": _clip(raw_doubt.get("after"))}
    return ModelResult("ok", data={"summary": summary[:SUMMARY_MAX_CHARS], "code_type_doubt": doubt,
                                   "doubt_dropped": dropped}, raw=content)


def parse_claim_effect(content: str, diff: Diff) -> ModelResult:
    try:
        obj = json.loads(_strip(content))
    except (ValueError, TypeError) as exc:
        return ModelResult("parse_failed", reason=f"not_json: {type(exc).__name__}", raw=content)
    if not isinstance(obj, dict) or set(obj) - EFFECT_OUTPUT_KEYS:
        return ModelResult("parse_failed", reason="effect_shape", raw=content)
    eff = obj.get("effect")
    if eff not in MODEL_EFFECTS:
        return ModelResult("parse_failed", reason=f"effect_not_in_list: {eff!r}", raw=content)
    quote = obj.get("after_quote")
    verified = _in_text(quote, diff.later_text) if quote else False
    return ModelResult("ok", data={"effect": eff, "after_quote": _clip(quote, 2000),
                                   "after_quote_verified": verified}, raw=content)


async def run_model(llm: Any, prompt: str, *, system: str = ASSESS_SYSTEM,
                    timeout_s: float = MODEL_TIMEOUT_S) -> tuple[Optional[str], Optional[str]]:
    """ONE core-plane call. Returns (content, error); never raises."""
    if llm is None:
        return None, "no_llm"
    try:
        resp = await asyncio.wait_for(
            llm.chat_complete([{"role": "user", "content": prompt}], system=system,
                              temperature=HOUSE_TEMPERATURE),
            timeout=timeout_s)
    except asyncio.TimeoutError:
        return None, "timeout"
    except Exception as exc:  # noqa: BLE001 — degrade, recorded
        logger.warning("evidence_drift_assess.model_call_failed err=%s", exc)
        return None, f"call_failed: {type(exc).__name__}"
    content = getattr(resp, "content", None)
    if not content:
        return None, f"empty_reply finish={getattr(resp, 'finish_reason', None)}"
    return str(content), None


def combine(code: Assessment, model: ModelResult) -> Assessment:
    """The assessment written when the model step ran: the CODE assessment
    (type, components, acknowledged, derived labels all unchanged), with the
    model's summary when clean and its verified doubt stored beside the code
    type. A failed reply leaves the code assessment and summary as they are,
    with the failure recorded."""
    import copy  # noqa: PLC0415

    out = copy.deepcopy(code)
    out.observations["model"] = {"status": model.status, "reason": model.reason}
    if model.status != "ok":
        return out
    out.summary = model.data["summary"]
    out.model_type_doubt = model.data.get("code_type_doubt")
    if model.data.get("doubt_dropped"):
        out.observations["model"]["doubt_dropped"] = model.data["doubt_dropped"]
    return out


def stamp(prompt_version: Optional[str], model_id: Optional[str]) -> str:
    """The assessor stamp: rules version | prompt version | model id, or
    ``|code_only``."""
    if not model_id:
        return f"{ASSESS_RULES_VERSION}|code_only"
    return f"{ASSESS_RULES_VERSION}|{prompt_version}|{model_id}"
