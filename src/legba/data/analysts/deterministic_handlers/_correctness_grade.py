# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G1 — THE GRADING PROTOCOL: one call per claim per family, and the ceiling.

A port of ``planning/PROGRAM1_2026-09-16/grade_p1.py``'s protocol onto the
platform's own LLM handlers. Nothing about the protocol moves — same three
families, same one-call-per-atom, same label set READ OUT OF the rubric, same
span check, same ONE corrective retry, same refusal to guess a label. What moves
is the transport: instead of a hand-rolled ``urllib`` POST this calls
``LLMProviderHandler.chat_complete`` on stack components the registry already
holds, so the key, the endpoint, the timeout and the price table are the
operator's config rather than this module's constants.

THE THREE FAMILIES (PREREG_P1 §4, unchanged):

  ===  ==========================================  =============================
  F0   gpt-oss-120b, the $0 self-hosted core plane  ``llm.primary.openai_compat``
  F2   meta-llama/llama-3.3-70b-instruct            ``llm.audit.openrouter_llama33_70b.openai_compat``
  F3   mistralai/mistral-medium-3.1                 ``llm.judge.openrouter_mistral_large.openai_compat``
  ===  ==========================================  =============================

F0 is DISCLOSED as the producer family: the same model writes the reads it is
here grading. VERDICT_P1v4 measured it as not the outlier (F0×F3 = 0.90 was the
strongest pair), and that disclosure travels with every number.

``max_tokens`` IS NEVER SENT — to any family. It is a HARD house rule for the
core plane (the model is served without one and sending it truncates reasoning
mid-object), and the calibration runs sent none to OpenRouter either, so sending
one here would change the instrument.

THE CEILING. ``LEGBA_GRADER_DAILY_CEILING_USD`` defaults to ``0``, at which F2
and F3 are NEVER CALLED — not resolved, not attempted, not estimated. Above
zero, the guard refuses the call that WOULD breach rather than reporting the
breach afterwards (``spent + the largest cost seen so far > ceiling``), which is
PREREG_P1 §5's rule and the only version of a ceiling that is one.

THE TRIAGE. F2/F3 run ONLY on claims F0 did not call ``silent``. A ``silent``
claim therefore carries ONE family's label. That is a real, stated limit: a
silent label lands in the coverage denominator and in NEITHER share's numerator,
so it can move coverage and can never move correctness — which is why the triage
is affordable. Every such claim is written ``single_family = true``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Mapping, Sequence

from ._correctness_rubric import LABELS, NOTE_TO_GRADER, RUBRIC_TEXT

logger = logging.getLogger(__name__)

#: The sentinel for a reply no attempt produced a label from. NEVER a guess:
#: the scorer counts it as disagreement, and it can only do that if this module
#: refuses to invent a label.
UNPARSEABLE = "UNPARSEABLE"

#: The output contract's own field names, in the rubric's order. Only
#: ``verdict`` is checked against a vocabulary; the rest are the grader's prose.
CONTRACT_FIELDS: tuple[str, ...] = (
    "verdict", "core_claim", "decisive_span", "reason",
)

#: Family order. F0 first because the triage depends on its answer.
FAMILY_ORDER: tuple[str, ...] = ("F0", "F2", "F3")
#: The PAID families — the ones the daily ceiling governs.
PAID_FAMILIES: tuple[str, ...] = ("F2", "F3")

#: Per-family registration. ``model`` is the id the calibration was run
#: against and is what ``grader_calibrations.model_ids`` is matched on;
#: ``component`` is the stack component the deps builder resolves;
#: ``price_*`` are the fallback cost model, used ONLY when the handler's own
#: ``usage.cost_estimate_usd`` comes back zero on a paid lane (an unpriced
#: component would otherwise make a paid run look free and defeat the ceiling).
FAMILIES: dict[str, dict[str, Any]] = {
    "F0": {
        "label": "F0 gpt-oss-120b (core plane)",
        "component": "llm.primary.openai_compat",
        # RESOLVED AT RUN TIME from the deployment's own core-plane model id —
        # see `core_model_id`. The literal here is only the shipped fallback.
        "model": None,
        "paid": False,
        "price_in": 0.0,
        "price_out": 0.0,
        "est_first_cost_usd": 0.0,
        "note": "PRODUCER FAMILY — the same model writes the reads it grades "
                "here. Disclosed in PREREG_P1 §4 and measured NOT to be the "
                "outlier (VERDICT_P1v4: F0xF3 = 0.90, the strongest pair).",
    },
    "F2": {
        "label": "F2 Meta Llama-3.3-70B (OpenRouter)",
        "component": "llm.audit.openrouter_llama33_70b.openai_compat",
        "model": "meta-llama/llama-3.3-70b-instruct",
        "paid": True,
        "price_in": 0.13,
        "price_out": 0.40,
        "est_first_cost_usd": 0.0015,
    },
    "F3": {
        # REPOINTED 2026-09-20. ``mistralai/mistral-large-2512`` — the id the
        # v4 gate ran against — was REMOVED from OpenRouter, so every F3 call
        # was a dead call against a model id that no longer resolves: the
        # silent-dead-analyst class, here silently making a three-family number
        # a two-family one. The component id is UNCHANGED
        # (``llm.judge.openrouter_mistral_large.openai_compat`` was repointed
        # live at the stack); only the model this table names and the prices
        # that back it move. Prices are OpenRouter list for
        # mistral-medium-3.1: $0.40 in / $2.00 out per million tokens.
        #
        # THIS BREAKS THE GATE ON PURPOSE. ``model_ids()`` returns this string
        # and the calibration lookup matches on it, so until a
        # ``grader_calibrations`` row covers mistral-medium-3.1 the job REFUSES
        # to publish rather than pooling a new model's labels into a number
        # calibrated on a different one. Re-gate with
        # ``scripts/correctness_regate.py``; nothing here seeds a row.
        "label": "F3 Mistral Medium 3.1 (OpenRouter)",
        "component": "llm.judge.openrouter_mistral_large.openai_compat",
        "model": "mistralai/mistral-medium-3.1",
        "paid": True,
        "price_in": 0.40,
        "price_out": 2.00,
        "est_first_cost_usd": 0.004,
    },
}

#: The core plane's SERVED model id. It is deployment config
#: (``LEGBA_LLM_MODEL_NAME``), not a constant, and the calibration lookup
#: matches on it — so an operator who repoints the core plane at a different
#: model fails the coverage test and stops the grader until a re-gate lands.
#: That is the point: F0's agreement with F2/F3 was measured on ONE model.
CORE_MODEL_ENV = "LEGBA_LLM_MODEL_NAME"
#: The model the v4 gate actually ran against on 2026-09-16, under its PUBLIC
#: name: a self-hosted ``gpt-oss-120b`` core plane. Used ONLY when the env is
#: unset, and named here rather than in a comment because the seeded calibration
#: row has to be able to say which model it calibrated. A deployment whose core
#: plane serves those weights under its own deployment-local id sets
#: ``CORE_MODEL_ENV`` to that id — the calibration lookup matches on the SERVED
#: string, so the served id, not this name, is what such a row records.
CORE_MODEL_DEFAULT = "gpt-oss-120b"


def core_model_id() -> str:
    """The core plane's served model id, from the deployment's own config."""
    return (os.getenv(CORE_MODEL_ENV) or "").strip() or CORE_MODEL_DEFAULT


def model_ids() -> dict[str, str]:
    """The model id per family, as this run would actually use them.

    The calibration lookup matches on these strings. A component repointed at a
    different model, or a core plane serving a different id, therefore fails the
    coverage test rather than quietly publishing numbers from an uncalibrated
    instrument.
    """
    return {
        family: (
            core_model_id() if cfg["model"] is None else str(cfg["model"])
        )
        for family, cfg in FAMILIES.items()
    }


class CeilingReached(RuntimeError):
    """The daily spend ceiling would be breached by the next paid call."""


# ---------------------------------------------------------------------------
# The span check (PREREG_P1 Amendment 2)
# ---------------------------------------------------------------------------

#: Curly quotes and apostrophes -> straight. References are wire copy and are
#: full of U+2019; a model that re-types a span with a straight apostrophe has
#: still quoted it verbatim, and failing that would measure the font, not the
#: rubric. NOTHING ELSE is folded: the check is case-sensitive, word for word.
_SPAN_QUOTES = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "′": "'", "´": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "″": '"',
}

#: The rubric's own decisive-span rule, whichever number it carries.
_SPAN_RULE_RE = re.compile(
    r"^\s*\d+\.\s+\*\*Decisive span[^*]*\*\*(.*?)(?=^\s*\d+\.\s+\*\*|\Z)",
    re.MULTILINE | re.DOTALL,
)
#: The sentence of that rule that says a span is left EMPTY.
_SPAN_EMPTY_SENTENCE_RE = re.compile(r"([^.]*\bempty\b[^.]*)\.?\s*$", re.DOTALL)

#: Words that may sit around a band-table quote without making it prose.
_BAND_FILLER = frozenset({
    "ref", "bands", "ref_bands", "band", "table", "dimension", "level",
    "is", "at", "the", "a", "an", "and",
})


def normalize_span(text: Any) -> str:
    """Whitespace collapsed, curly quotes straightened. Case-sensitive.

    The ONLY normalisation the span check applies, on both sides of the
    comparison. Anything more (lowercasing, stripping punctuation) would start
    accepting paraphrases, which is the thing being measured.
    """
    if not isinstance(text, str):
        return ""
    out = text
    for curly, straight in _SPAN_QUOTES.items():
        out = out.replace(curly, straight)
    return " ".join(out.split())


def developments_haystack(reference: Mapping[str, Any] | None) -> str:
    """The developments' ``summary`` + ``decisive_span``, concatenated, normalised.

    The band table is deliberately NOT in here: the rubric makes a band-table
    span malformed, and the cheapest way to enforce that is never to offer the
    bands as a source.
    """
    devs = (reference or {}).get("ref_developments") or []
    return normalize_span("\n".join(
        str(dev.get(field) or "")
        for dev in devs
        for field in ("summary", "decisive_span")
    ))


def is_band_table_span(span: Any, reference: Mapping[str, Any] | None) -> bool:
    """Does this span look lifted from the band table?

    A HEURISTIC, consulted ONLY after the verbatim check has already failed — so
    it can never decide whether a span passes, only how the retry NAMES the
    failure.
    """
    normalised = normalize_span(span)
    if not normalised:
        return False
    bands = (reference or {}).get("ref_bands") or {}
    if not isinstance(bands, dict) or not bands:
        return False
    rendered = normalize_span(
        json.dumps(bands, sort_keys=True, ensure_ascii=False)
    )
    if normalised in rendered:
        return True
    dims: set[str] = set()
    vocab: set[str] = set(_BAND_FILLER)
    for key, value in bands.items():
        for word, bucket in ((str(key).lower(), dims), (str(value).lower(), vocab)):
            bucket.add(word)
            bucket.update(t for t in re.split(r"[^a-z0-9]+", word) if t)
    vocab |= dims
    tokens = [t for t in re.split(r"[^A-Za-z0-9_]+", normalised.lower()) if t]
    return (
        bool(tokens)
        and all(t in vocab for t in tokens)
        and any(t in dims for t in tokens)
    )


def span_policy(
    rubric: str = RUBRIC_TEXT, labels: Sequence[str] = LABELS
) -> dict[str, frozenset[str]]:
    """Which labels the RUBRIC ITSELF requires a span for, and which require empty.

    Read out of its decisive-span rule, not hardcoded — same discipline as the
    label extraction. A policy typed in here would keep passing the day the
    rubric moved the rule. Raises if the rule cannot be read or does not account
    for every label exactly once.
    """
    match = _SPAN_RULE_RE.search(rubric or "")
    if not match:
        raise ValueError(
            "the rubric has no `**Decisive span**` rule this code can read — "
            "refusing to invent a span policy for a frozen rubric."
        )
    block = match.group(1)
    empty_match = _SPAN_EMPTY_SENTENCE_RE.search(block)
    if not empty_match:
        raise ValueError(
            "the rubric's decisive-span rule never says which label leaves the "
            "span EMPTY — refusing to guess which one it is."
        )
    empty_part = empty_match.group(1)
    required_part = block[:empty_match.start(1)]
    forbid = frozenset(
        lbl for lbl in labels
        if re.search(rf"\b{re.escape(lbl)}\b", empty_part)
    )
    require = frozenset(
        lbl for lbl in labels
        if re.search(rf"\b{re.escape(lbl)}\b", required_part)
    )
    if (
        not require or not forbid or (require & forbid)
        or (require | forbid) != set(labels)
    ):
        raise ValueError(
            f"the rubric's decisive-span rule does not account for its own "
            f"labels exactly once: requires a span for {sorted(require)}, "
            f"requires empty for {sorted(forbid)}, labels {list(labels)}."
        )
    return {"require": require, "forbid": forbid}


def span_check(
    verdict: str,
    span: Any,
    reference: Mapping[str, Any] | None,
    policy: Mapping[str, frozenset[str]],
) -> tuple[bool, str | None, str | None]:
    """``(ok, failure code, the sentence that goes back to the model)``.

    The failure is NAMED, because "your span is wrong" and "your span is the
    band table" are different corrections and the retry only gets one shot.
    """
    normalised = normalize_span(span)
    if verdict in policy["forbid"]:
        if normalised:
            return False, "nonempty_on_empty_label", (
                f"`decisive_span` must be EMPTY for `{verdict}` — the rubric "
                "leaves the span empty for that label, and you returned one."
            )
        return True, None, None
    if verdict not in policy["require"]:
        return True, None, None
    if not normalised:
        # A labelled verdict must QUOTE the development that decided it. An
        # empty string is a substring of everything, so letting it through
        # would stamp `span_unverified: false` on a row where nothing was
        # verified at all.
        return False, "empty", (
            f"`decisive_span` is empty; `{verdict}` requires the ONE "
            "development sentence or fragment that decided it, quoted verbatim."
        )
    if normalised in developments_haystack(reference):
        return True, None, None
    if is_band_table_span(span, reference):
        return False, "band_table", (
            "`decisive_span` is the band table; the rubric makes a span quoted "
            "from the band table malformed — bands are not evidence."
        )
    return False, "not_verbatim", (
        "`decisive_span` is not verbatim from any development — it must be "
        "copied word for word out of one development's `summary` or "
        "`decisive_span` in the item you were given."
    )


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def build_system_message(
    note: str = NOTE_TO_GRADER, rubric: str = RUBRIC_TEXT
) -> str:
    """note_to_grader FIRST, then the rubric VERBATIM (Amendment 2).

    The rubric ALREADY ENDS in its output contract; v3 appended a second copy
    underneath, and a duplicated instruction is a second source of truth that
    can drift from the frozen rubric. Nothing is paraphrased and nothing is
    added: the packet is the whole of the instruction, which is what makes
    three families comparable.
    """
    return "\n\n".join([note.strip(), rubric.rstrip()])


def build_user_message(item: Mapping[str, Any]) -> str:
    """ONE atom: the assertion and its committed reference, nothing else.

    ``p1_id`` is deliberately NOT sent. This code already owns the id with
    certainty and stamps it on the output row itself; asking the model to echo a
    value we hold is an invitation to a mismatch nobody catches.
    """
    return json.dumps(
        {"assertion": item["assertion"], "reference": item["reference"]},
        sort_keys=True, ensure_ascii=False, indent=2,
    )


def retry_message(
    reasons: Sequence[str], labels: Sequence[str], *, span_only: bool
) -> str:
    """The ONE corrective turn, naming the exact failure.

    A span failure and a bad label need different corrections: telling a model
    that already returned ``contains`` that its verdict must be one of three
    labels teaches it nothing about the span it got wrong.
    """
    parts = [
        "Your reply did not satisfy the output contract: "
        + "; ".join(reasons) + "."
    ]
    if span_only:
        parts.append(
            "Keep the verdict if it is right and FIX THE SPAN: `decisive_span` "
            "must be copied word for word out of one development's `summary` "
            "or `decisive_span` in the item you were given — never from the "
            "band table, never paraphrased."
        )
    else:
        parts.append(f"`verdict` must be exactly one of {list(labels)}.")
    parts.append(
        "Return ONLY one JSON object with the fields "
        + ", ".join(CONTRACT_FIELDS)
        + " and nothing else — no prose, no markdown code fence."
    )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------

#: Full-width punctuation the core-plane models emit. The 【N】 citation bracket
#: is a known house artefact; the full-width braces/quotes are the same class of
#: CJK-font substitution and would otherwise make good JSON unparseable.
_FULLWIDTH = {
    "｛": "{", "｝": "}", "［": "[", "］": "]", "＂": '"',
    "“": '"', "”": '"', "：": ":", "，": ",",
}
_CITATION_BRACKET_RE = re.compile(r"【[^】]{0,40}】")
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*|\s*```\s*$")


def normalize_reply(content: str) -> str:
    """Whitespace, a code fence, full-width punctuation and 【N】 markers
    removed — nothing else. This never rewrites the model's words."""
    out = content or ""
    out = _CITATION_BRACKET_RE.sub("", out)
    for wide, ascii_char in _FULLWIDTH.items():
        out = out.replace(wide, ascii_char)
    return _FENCE_RE.sub("", out.strip()).strip()


def extract_json_object(content: str) -> dict[str, Any] | None:
    """THE FIRST top-level JSON object in the reply, or ``None``. Never a
    partial guess. An outer find/rfind attempt, then a balanced-brace walk for
    the case where prose trails the object."""
    text = normalize_reply(content)
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    end = text.rfind("}")
    if end > start:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            pass
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                except (ValueError, TypeError):
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def validate(
    obj: Mapping[str, Any] | None, labels: Sequence[str]
) -> tuple[dict[str, Any] | None, list[str]]:
    """``(the contract fields, [])`` or ``(None, reasons)``.

    Only ``verdict`` is checked against a vocabulary — ``core_claim`` /
    ``decisive_span`` / ``reason`` are the grader's own prose and a missing one
    is recorded, not invented.
    """
    if not isinstance(obj, Mapping):
        return None, ["reply did not contain a parseable JSON object"]
    verdict = obj.get("verdict")
    if not isinstance(verdict, str) or not verdict.strip():
        return None, ["`verdict` missing or not a string"]
    verdict = verdict.strip()
    if verdict not in labels:
        return None, [
            f"verdict {verdict!r} is not one of the rubric's three labels "
            f"{list(labels)}"
        ]
    return {
        "verdict": verdict,
        "core_claim": obj.get("core_claim"),
        "decisive_span": obj.get("decisive_span"),
        "reason": obj.get("reason"),
    }, []


# ---------------------------------------------------------------------------
# The ceiling
# ---------------------------------------------------------------------------

CEILING_ENV = "LEGBA_GRADER_DAILY_CEILING_USD"
DEFAULT_CEILING_USD = 0.0


def daily_ceiling_usd() -> float:
    """``LEGBA_GRADER_DAILY_CEILING_USD`` — default ``0`` (F0-only, $0).

    An unparseable or negative value reads as ZERO, never as "unlimited": a
    typo in an operator's env must fail toward not spending their money.
    """
    raw = (os.getenv(CEILING_ENV) or "").strip()
    if not raw:
        return DEFAULT_CEILING_USD
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "correctness_grader.ceiling_unparseable raw=%r — reading as $0 "
            "(F0-only). A typo must fail toward not spending.", raw,
        )
        return DEFAULT_CEILING_USD
    return max(0.0, value)


def would_breach(spent: float, ceiling: float, est_next: float) -> bool:
    """True if the NEXT call could take the day past the ceiling.

    NOT ``spent >= ceiling``: that lets the call that actually breaches go out
    first and reports the breach afterwards. PREREG_P1 §5 says stop BEFORE the
    call that would breach, so the estimate of the next call — the largest cost
    seen so far, or the family's registered first-call estimate — is added in.
    """
    return (spent + max(0.0, est_next)) > ceiling


def call_cost_usd(family: str, usage: Any) -> tuple[float, str]:
    """``(cost, how it was derived)`` for one call.

    The handler's own ``usage.cost_estimate_usd`` wins — it is computed from the
    component's configured price, which is the operator's number. It is only
    fallen back on for a PAID family that reported zero against non-zero tokens:
    an unpriced component would otherwise make a paid run look free, and a
    ceiling that cannot see spend is not a ceiling.
    """
    cfg = FAMILIES[family]
    if not cfg["paid"]:
        return 0.0, "core_plane_zero"
    stamped = float(getattr(usage, "cost_estimate_usd", 0.0) or 0.0)
    if stamped > 0.0:
        return round(stamped, 6), "handler_usage"
    tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
    if not (tokens_in or tokens_out):
        return 0.0, "no_usage_reported"
    fallback = (
        tokens_in * float(cfg["price_in"]) / 1e6
        + tokens_out * float(cfg["price_out"]) / 1e6
    )
    return round(fallback, 6), "registered_price_fallback"


# ---------------------------------------------------------------------------
# Grading one claim with one family
# ---------------------------------------------------------------------------


async def _call_once(
    llm: Any, system_message: str, messages: Sequence[Mapping[str, Any]]
) -> Any:
    """One completion. ``max_tokens`` is NEVER passed — see the module docstring."""
    return await llm.chat_complete(
        list(messages), system=system_message, temperature=1.0,
    )


async def grade_one(
    item: Mapping[str, Any],
    family: str,
    llm: Any,
    *,
    labels: Sequence[str] = LABELS,
    system_message: str | None = None,
    policy: Mapping[str, frozenset[str]] | None = None,
) -> dict[str, Any]:
    """One claim, one family, up to two attempts, always exactly ONE output row.

    TWO ways a reply can fail, and Amendment 2 does not treat them the same:

      * the VERDICT is outside the rubric's label set — malformed. One
        corrective retry; still malformed and the claim is ``UNPARSEABLE``,
        never guessed.
      * the LABEL is good but the DECISIVE SPAN is not verbatim from a
        development (or is the band table) — one corrective retry naming that
        failure exactly. If the retry's span fails too, THE LABEL STANDS and the
        row carries ``span_unverified: true``. A span quibble must never cost a
        label the grader actually gave; the scorer gates on labels and reports
        the unverified spans beside them.

    Both share the ONE corrective retry §5 registers — never two. A reply that
    carried a valid label on ANY attempt is never written ``UNPARSEABLE``.
    """
    system_message = (
        build_system_message() if system_message is None else system_message
    )
    policy = span_policy() if policy is None else policy
    reference = item.get("reference") or {}
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": build_user_message(item)}
    ]
    attempts: list[dict[str, Any]] = []
    best: tuple[dict[str, Any], bool, str | None] | None = None

    for attempt in (1, 2):
        content = ""
        usage: Any = None
        error: str | None = None
        try:
            response = await _call_once(llm, system_message, messages)
            content = str(getattr(response, "content", "") or "")
            usage = getattr(response, "usage", None)
        except Exception as exc:  # noqa: BLE001 — a dead family is a finding
            error = f"{type(exc).__name__}: {str(exc)[:250]}"
            logger.warning(
                "correctness_grader.call_failed family=%s claim=%s err=%s",
                family, item.get("p1_id"), error,
            )
        cost, cost_source = (
            call_cost_usd(family, usage) if usage is not None else (0.0, "no_call")
        )
        obj = extract_json_object(content) if not error else None
        fields, reasons = validate(obj, labels)
        span_ok, span_code, span_msg = True, None, None
        if fields is not None:
            span_ok, span_code, span_msg = span_check(
                fields["verdict"], fields.get("decisive_span"), reference, policy
            )
            if not span_ok and span_msg:
                reasons = [*reasons, span_msg]
        if error:
            reasons = [error, *reasons]
        attempts.append({
            "usage": usage, "cost_usd": cost, "cost_source": cost_source,
            "error": error, "reasons": reasons, "span_failure": span_code,
        })
        if fields is not None:
            best = (fields, span_ok, span_code)
            if span_ok:
                return _row(item, family, fields, attempts,
                            retried=attempt > 1, span_unverified=False,
                            span_failure=None)
        if attempt == 2:
            break
        if not error:
            # THE ONE CORRECTIVE RETRY, in the same history: the model sees its
            # own bad reply and the exact reason it failed.
            messages = [
                *messages,
                {"role": "assistant", "content": content},
                {"role": "user", "content": retry_message(
                    reasons, labels, span_only=fields is not None)},
            ]
        # A transport failure is not a malformed reply; retry the same request
        # rather than scolding a model that never answered.

    if best is not None:
        fields, span_ok, span_code = best
        return _row(item, family, fields, attempts, retried=True,
                    span_unverified=not span_ok, span_failure=span_code)
    return _row(
        item, family,
        {"verdict": UNPARSEABLE, "core_claim": None, "decisive_span": None,
         "reason": "; ".join(
             r for a in attempts for r in a["reasons"])[:500] or None},
        attempts, retried=True, span_unverified=False, span_failure=None,
    )


def _row(
    item: Mapping[str, Any],
    family: str,
    fields: Mapping[str, Any],
    attempts: Sequence[Mapping[str, Any]],
    *,
    retried: bool,
    span_unverified: bool,
    span_failure: str | None,
) -> dict[str, Any]:
    last = attempts[-1]
    usage = last.get("usage")
    return {
        "claim_id": item["p1_id"],
        "family": family,
        **dict(fields),
        "span_unverified": bool(span_unverified),
        "span_failure": span_failure,
        "error": last.get("error"),
        "tokens_in": int(getattr(usage, "prompt_tokens", 0) or 0),
        "tokens_out": int(getattr(usage, "completion_tokens", 0) or 0),
        "model": str(getattr(usage, "model", "") or ""),
        "cost_usd": round(
            sum(float(a.get("cost_usd") or 0.0) for a in attempts), 6
        ),
        "cost_source": last.get("cost_source"),
        "retried": bool(retried),
    }


__all__ = [
    "CEILING_ENV",
    "CONTRACT_FIELDS",
    "CeilingReached",
    "DEFAULT_CEILING_USD",
    "FAMILIES",
    "FAMILY_ORDER",
    "CORE_MODEL_DEFAULT",
    "CORE_MODEL_ENV",
    "PAID_FAMILIES",
    "UNPARSEABLE",
    "build_system_message",
    "build_user_message",
    "call_cost_usd",
    "core_model_id",
    "daily_ceiling_usd",
    "developments_haystack",
    "extract_json_object",
    "grade_one",
    "is_band_table_span",
    "model_ids",
    "normalize_reply",
    "normalize_span",
    "retry_message",
    "span_check",
    "span_policy",
    "validate",
    "would_breach",
]
