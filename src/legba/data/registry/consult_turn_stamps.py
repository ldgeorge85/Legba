# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two stamps every persisted consult turn carries.

* the USAGE RECEIPT (``consult_turns.usage``, migration 0195) — tokens in and
  out, calls, model, route, elapsed, and ``cost_usd`` ONLY when the route
  prices its calls (or, when no pricing flag came back but the loop's price
  table produced a non-zero estimate, that estimate with ``cost_reason:
  "table_estimate"``); otherwise ``cost_usd: null`` with ``cost_reason``. A field
  the run did not report is ``null`` with ``reason``, never a zero: on
  2026-10-08 every one of the 91 live assistant turns stored ``{}`` because
  the chat writer never passed the receipt it had (W1-F1).
* the VERIFICATION STAMP (``consult_turns.verification``, migration 0234) —
  ``judged`` with a score, ``unassessable`` with a reason (never 0.5 —
  W11-F29), or ``not_judged`` with a reason. Never blank: a turn whose run
  produced no stamp (a cut run, a deep task, a runtime that predates judging)
  is labelled here, at the front door, with why.

A leaf: no imports from the rest of the registry, nothing from the runtime —
it must import under the slim image.

W11-F93 adds two things the analyst's judge and this front door share, so they
live in the leaf both images import:

* the WHY-NOT vocabulary — ``judge_unavailable_reason`` on every stamp whose
  judge could not run: ``budget_exhausted`` | ``route_down`` | ``timeout`` |
  ``not_settled``. A bare "judge unavailable" is never the whole label again.
* the SETTLE DIGEST — the judge stamps ``settled_sha256`` over the exact answer
  and cited-ref set it graded; :func:`verification_stamp` re-derives it from
  the answer the front door is about to persist and withdraws a verdict whose
  text moved after judging (``not_settled``). A verdict is about the persisted
  turn, or it is not a verdict.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

__all__ = [
    "UNAVAILABLE_BUDGET_EXHAUSTED",
    "UNAVAILABLE_NOT_SETTLED",
    "UNAVAILABLE_REASONS",
    "UNAVAILABLE_ROUTE_DOWN",
    "UNAVAILABLE_TIMEOUT",
    "USAGE_SCHEMA",
    "VERIFICATION_SCHEMA",
    "model_from_calls",
    "settle_digest",
    "unavailable_reason_for",
    "unavailable_reason_for_class",
    "usage_from_steps",
    "usage_receipt",
    "verification_stamp",
]

USAGE_SCHEMA = "consult_usage/1"
#: Mirrors ``analysts.consult_judging.VERIFICATION_SCHEMA`` (not imported: the
#: registry image must not pull the analyst package for a string).
VERIFICATION_SCHEMA = "consult_verification/1"
_STATES = frozenset({"judged", "unassessable", "not_judged"})

#: Why a judge could not run. Closed: a stamp carries one of these or
#: no ``judge_unavailable_reason`` at all (a deliberate state — the flag off, a
#: deep answer — is not unavailability).
UNAVAILABLE_BUDGET_EXHAUSTED = "budget_exhausted"
UNAVAILABLE_ROUTE_DOWN = "route_down"
UNAVAILABLE_TIMEOUT = "timeout"
UNAVAILABLE_NOT_SETTLED = "not_settled"
UNAVAILABLE_REASONS = frozenset({
    UNAVAILABLE_BUDGET_EXHAUSTED, UNAVAILABLE_ROUTE_DOWN,
    UNAVAILABLE_TIMEOUT, UNAVAILABLE_NOT_SETTLED,
})

#: A stamp ``reason`` → its why-not. Also the back-fill for stamps written by a
#: runtime that predates the field (the front door labels those, too).
_REASON_TO_UNAVAILABLE: dict[str, str] = {
    "synthesis_incomplete": UNAVAILABLE_NOT_SETTLED,
    "judged_text_changed": UNAVAILABLE_NOT_SETTLED,
    "judge_timeout": UNAVAILABLE_TIMEOUT,
    "judge_input_over_budget": UNAVAILABLE_BUDGET_EXHAUSTED,
    "no_judge_route": UNAVAILABLE_ROUTE_DOWN,
    "judge_route_unresolved": UNAVAILABLE_ROUTE_DOWN,
    "no_evidence_reader": UNAVAILABLE_ROUTE_DOWN,
    "judge_failed": UNAVAILABLE_ROUTE_DOWN,
    "judge_unavailable": UNAVAILABLE_ROUTE_DOWN,
}

#: A judge-failure class (``provenance.judge_failure_class``) → its why-not. A
#: 402 / insufficient-credits is the judge route's own budget running out.
_CLASS_TO_UNAVAILABLE: dict[str, str] = {
    "timeout": UNAVAILABLE_TIMEOUT,
    "credit": UNAVAILABLE_BUDGET_EXHAUSTED,
    "budget": UNAVAILABLE_BUDGET_EXHAUSTED,
}


def unavailable_reason_for_class(cls: Any) -> str:
    """The why-not for one judge-failure class; anything else is ``route_down``."""
    return _CLASS_TO_UNAVAILABLE.get(str(cls or ""), UNAVAILABLE_ROUTE_DOWN)


def unavailable_reason_for(stamp: Mapping[str, Any]) -> str | None:
    """The why-not a non-judged stamp carries, derived when it carries none.

    ``None`` for a stamp that is not a failure to judge (judged, unassessable,
    ``judge_disabled``, a deep answer, a run that left no stamp at all).
    """
    given = stamp.get("judge_unavailable_reason")
    if given in UNAVAILABLE_REASONS:
        return str(given)
    reason = str(stamp.get("reason") or "")
    if reason == "judge_unavailable":
        unverified = stamp.get("unverified_reason")
        if isinstance(unverified, Mapping) and unverified.get("class"):
            return unavailable_reason_for_class(unverified["class"])
    return _REASON_TO_UNAVAILABLE.get(reason)


def settle_digest(answer: Any, cited_refs: Sequence[Any] | None) -> str:
    """sha256 over the answer text and its cited-ref SET (order-free, case-free).

    The judge stamps it over what it graded; the front door re-derives it over
    what it persists. Equal ⇒ the verdict is about the persisted turn.
    """
    refs = sorted({str(r).strip().lower() for r in (cited_refs or []) if r})
    h = hashlib.sha256(str(answer or "").encode("utf-8"))
    h.update(b"\x00")
    h.update("\n".join(refs).encode("utf-8"))
    return h.hexdigest()


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def usage_from_steps(steps: Sequence[Any]) -> dict[str, Any]:
    """The newest spend meter a run's step frames carried, or ``{}``.

    A cut or failed run never reaches the receipt, but every ``llm_call``
    frame carried the running meter; the last one IS what the run spent.
    """
    for step in reversed(list(steps or [])):
        if isinstance(step, Mapping) and isinstance(step.get("usage"), Mapping):
            return dict(step["usage"])
    return {}


#: What one per-call receipt on a consult turn keeps: the run
#: account's ``llm_calls`` entry (``stack/llm/base._account_call``) cut to the
#: fields a reader of the turn needs — which alias answered, what it cost in
#: tokens, how long it queued at the core-plane gate and how long it took.
#: Never the prompt or completion text (those stay on the trace path).
CALL_RECEIPT_INT_KEYS: tuple[str, ...] = (
    "prompt_tokens", "completion_tokens", "reasoning_tokens", "gate_wait_ms",
    "duration_ms", "tool_call_count",
)
CALL_RECEIPT_STR_KEYS: tuple[str, ...] = (
    "model", "component_id", "subprovider", "status", "finish_reason",
    "thinking_fallback", "error",
)
#: Sparse: present only when the call carried them (an Anthropic call
#: whose ``max_tokens`` the context window limited — an API validity constraint).
CALL_RECEIPT_SPARSE_KEYS: tuple[str, ...] = (
    "max_tokens_sent", "max_tokens_model_max", "max_tokens_limited_by",
)
#: A turn's receipts are bounded (a runaway loop never writes an unbounded row).
MAX_CALL_RECEIPTS = 64


def call_receipts(raw: Any) -> list[dict[str, Any]] | None:
    """Normalise the run account's per-call entries into turn receipts.

    ``None`` when the run carried none (an older runtime, or a path with no
    account bound — the deep workflow), so "not recorded" never reads as
    "zero calls". ``gate_wait_ms`` is sparse on the account (recorded only
    when the call waited); here it is always present, 0 when it did not.
    ``reasoning_tokens`` stays ``None`` when the gateway did not report it.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or "truncated" in entry and len(entry) == 1:
            continue
        row: dict[str, Any] = {}
        for k in CALL_RECEIPT_STR_KEYS:
            v = entry.get(k)
            row[k] = v if isinstance(v, str) and v else None
        for k in CALL_RECEIPT_INT_KEYS:
            row[k] = _int_or_none(entry.get(k))
        if row["gate_wait_ms"] is None:
            row["gate_wait_ms"] = 0
        row["thinking"] = entry.get("thinking") is True
        row.update({k: entry[k] for k in CALL_RECEIPT_SPARSE_KEYS if entry.get(k) is not None})
        out.append(row)
        if len(out) >= MAX_CALL_RECEIPTS:
            break
    return out


def usage_receipt(
    raw: Any,
    *,
    plane: str | None,
    route: str | None,
    reason_if_absent: str = "usage_not_reported",
) -> dict[str, Any]:
    """Normalise a run's usage into the persisted ``consult_usage/1`` receipt.

    ``raw`` is the runtime's receipt (``SpendGuard.receipt``), its older meter
    (``snapshot`` — no model/elapsed/priced), or nothing. ``plane`` is the
    operator's friendly pick (``core`` / ``opus`` / ``fable``) and ``route``
    the stack component it maps to — both known only at the front door.
    """
    src: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    measured = bool(src)
    priced = src.get("priced")
    est = _float_or_none(src.get("est_cost_usd"))
    if priced is True and est is not None:
        cost, cost_reason = round(est, 6), None
    elif priced is False:
        # A route without a price table (the self-hosted core plane) is not
        # "free by estimate"; its cost is simply not priced. Say so.
        cost, cost_reason = None, "route_unpriced"
    elif measured and est is not None and est > 0:
        # The provider reported no pricing flag, but the loop's own price
        # table produced an estimate (a cut run's last ``llm_call`` meter, or
        # an older receipt). Visible cost is a rule on a paid path (ledger
        # 09-16): stamp the estimate and say it is one, rather than ``None``.
        # A ZERO estimate stays unpriced — on a route with no price table that
        # number means "not priced", never "free".
        cost, cost_reason = round(est, 6), "table_estimate"
    elif measured:
        cost, cost_reason = None, "pricing_not_reported"
    else:
        cost, cost_reason = None, reason_if_absent
    receipts = call_receipts(src.get("call_receipts"))
    configured = src.get("model") if isinstance(src.get("model"), str) else None
    model, models = model_from_calls(receipts, configured)
    return {
        "schema": USAGE_SCHEMA,
        "plane": plane,
        "route": route,
        # What RAN, from the per-call receipts (``model_from_calls``);
        # the handler's configured name only when the run carried no receipts.
        "model": model,
        "models": models,
        "configured_model": configured,
        "subprovider": (
            src.get("subprovider") if isinstance(src.get("subprovider"), str) else None
        ),
        "calls": _int_or_none(src.get("calls")),
        "input_tokens": _int_or_none(src.get("input_tokens")),
        "output_tokens": _int_or_none(src.get("output_tokens")),
        "elapsed_s": _float_or_none(src.get("elapsed_s")),
        "cost_usd": cost,
        "cost_reason": cost_reason,
        # The meter keys the panel's live spend line already reads.
        "est_cost_usd": est,
        "max_input_tokens": _int_or_none(src.get("max_input_tokens")),
        "max_cost_usd": _float_or_none(src.get("max_cost_usd")),
        # The per-TURN ceiling the run was held to (``TurnCeilingGuard``'s
        # snapshot; registry/consult_turn_ceiling.py documents it here). ``null``
        # when no ceiling was armed: the knob off, or an unpriced route.
        "turn_ceiling_usd": _float_or_none(src.get("turn_ceiling_usd")),
        # The per-call output that ceiling PROJECTED with, an
        # estimate with its basis (the route's largest observed output, else
        # the fallback). ``null`` when no ceiling was armed.
        "turn_projection": (
            {"output_tokens": _int_or_none(src.get("projected_output_tokens")),
             "basis": str(src.get("projection_basis"))}
            if src.get("projection_basis") else None
        ),
        "reason": None if measured else reason_if_absent,
        # One receipt per model call (``call_receipts``); ``null``
        # when the run carried none.
        "call_receipts": receipts,
    }


def model_from_calls(
    receipts: list[dict[str, Any]] | None, configured: str | None,
) -> tuple[str | None, dict[str, int] | None]:
    """The turn's ``model`` and ``models`` from its per-call receipts.

    Example: every call ran the thinking alias ``deep``, while
    the turn said ``fast`` — the handler's CONFIGURED name, which the receipt
    took from the handler, not from the calls (the thinking switch picks the
    alias per call). With receipts the turn says what ran: the one model when
    every call that named one agrees, else ``"mixed: deep ×3, fast ×1"`` (most
    calls first) — never one picked from several. ``models`` counts the calls
    per model. No receipts (an older run, or none carried): the configured
    name, and ``models`` ``None``.
    """
    if not receipts:
        return configured, None
    counts: dict[str, int] = {}
    for r in receipts:
        m = r.get("model")
        if isinstance(m, str) and m:
            counts[m] = counts.get(m, 0) + 1
    if not counts:
        return configured, None
    if len(counts) == 1:
        return next(iter(counts)), counts
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return "mixed: " + ", ".join(f"{m} ×{n}" for m, n in ordered), dict(ordered)


def _label(reason: str, **extra: Any) -> dict[str, Any]:
    stamp: dict[str, Any] = {
        "schema": VERIFICATION_SCHEMA, "state": "not_judged",
        "score": None, "reason": reason, **extra,
    }
    why = unavailable_reason_for(stamp)
    if why is not None:
        stamp["judge_unavailable_reason"] = why
    return stamp


def verification_stamp(
    raw: Any,
    *,
    reason_if_absent: str,
    answer: Any = None,
    cited_refs: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """The run's ``consult_verification/1`` stamp, or the explicit label.

    A stamp the runtime wrote passes through, with the invariants enforced
    here because the front door is the last place they can be: a non-``judged``
    state carries NO score (an unassessable turn is never 0.5 and a floor-only
    turn is never published as a verdict), a ``judged`` state without a
    numeric score is not a verdict at all, a non-judged state that is a failure
    to judge says why (``judge_unavailable_reason``), and — when ``answer`` is
    given — a verdict whose ``settled_sha256`` does not match the answer about
    to be persisted is withdrawn as ``not_settled``.
    """
    if isinstance(raw, Mapping) and raw.get("state") in _STATES:
        stamp = dict(raw)
        stamp.setdefault("schema", VERIFICATION_SCHEMA)
        if stamp["state"] != "judged":
            stamp["score"] = None
            stamp["reason"] = stamp.get("reason") or stamp["state"]
            if stamp["state"] == "not_judged":
                why = unavailable_reason_for(stamp)
                if why is not None:
                    if stamp.get("judge_unavailable_reason") not in (None, why):
                        # An older runtime wrote the verify pass's raw reason
                        # here (``judge_error``); keep it, under its own name.
                        stamp["verify_unavailable_reason"] = stamp["judge_unavailable_reason"]
                    stamp["judge_unavailable_reason"] = why
            return stamp
        score = _float_or_none(stamp.get("score"))
        if score is None:
            return _label("judged_without_score")
        digest = stamp.get("settled_sha256")
        # Q-1 (rule D): a record-only grounding pass edits the judged text in
        # a recorded, deterministic way and chains the digest — judged text
        # (``input_sha256`` = ``settled_sha256``) → persisted text
        # (``output_sha256``). The verdict stands for the persisted turn only
        # when both links hold.
        grounding = stamp.get("grounding") if isinstance(stamp.get("grounding"), Mapping) else {}
        chained = (
            answer is not None and isinstance(digest, str) and digest
            and grounding.get("input_sha256") == digest
            and grounding.get("output_sha256") == settle_digest(answer, cited_refs)
        )
        if (
            answer is not None and isinstance(digest, str) and digest and not chained
            and digest != settle_digest(answer, cited_refs)
        ):
            # The judge graded text the turn no longer carries: whatever it
            # said, it did not say it about THIS answer.
            return _label(
                "judged_text_changed",
                judge_llm_ref=stamp.get("judge_llm_ref"),
                judged_at=stamp.get("judged_at"),
            )
        stamp["score"] = score
        return stamp
    return _label(reason_if_absent)
