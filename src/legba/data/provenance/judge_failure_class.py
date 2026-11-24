# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""JUDGE-2 (W11b) — the FAILURE CLASS of one LLM call, in one place.

THE INCIDENT. OpenRouter answered HTTP 402 (out of credits) on every judge call
from 2026-10-02 23:08Z to 2026-10-05 02:03Z, about 51 hours. The transport shim
(:mod:`.judge_transport`) correctly refused to retry a 402 — a second request
cannot top up an account — and the verify pass correctly fell to the
deterministic floor. What nothing did was tell the difference between "the
judge is busy" (429/5xx/timeout: wait and try the SAME route again) and "the
judge route is gone" (401/402/403/404: no amount of waiting helps; another
route might). Three readers need that difference and must agree on it:

  * the critic's judge FALLBACK CHAIN (``runtime/judge_fallback_chain.py``)
    moves to the next rung on a route-gone class and ONLY on that class;
  * the liveness watchdog pages AT ONCE on a route-gone class from a hosted
    component (``runtime/llm_route_liveness.py``);
  * the judge health read (``data/registry/judge_stats_api.py``) names the
    class of a component's newest failure.

A LEAF: stdlib only, so the slim registry image can import it beside
``judge_transport`` without pulling the runtime package.
"""

from __future__ import annotations

import re
from typing import Any

#: The HTTP statuses that mean THE ROUTE IS GONE, not "busy": a revoked or
#: missing key (401), an empty account (402), a policy refusal (403), a model id
#: the provider no longer serves (404). A retry on the same route cannot change
#: any of them; a different route can.
ROUTE_GONE_STATUSES: frozenset[int] = frozenset({401, 402, 403, 404})

#: Failure classes. ``auth`` / ``credit`` / ``retired`` are ROUTE-GONE (the chain
#: moves, the watchdog pages); the rest stay on the route with today's retry.
CLASS_AUTH = "auth"
CLASS_CREDIT = "credit"
CLASS_RETIRED = "retired"
CLASS_RATE_LIMITED = "rate_limited"
CLASS_SERVER = "server_error"
CLASS_TIMEOUT = "timeout"
CLASS_EMPTY = "empty"
CLASS_UNRESOLVABLE = "unresolvable"
CLASS_ERROR = "error"

ROUTE_GONE_CLASSES: frozenset[str] = frozenset({CLASS_AUTH, CLASS_CREDIT, CLASS_RETIRED})

#: The provider's insufficient-credits wording, for the case where the body
#: says it but the status does not (a router wrapping the upstream refusal in a
#: 400, or a 403 whose real meaning is the balance). Matched case-insensitively
#: against the exception message and body, never logged.
_INSUFFICIENT_CREDITS_RE = re.compile(
    r"insufficient[ _-]?(credits?|funds|balance|quota)|requires more credits|"
    r"credit balance is too low|out of credits|payment required",
    re.IGNORECASE,
)

_TIMEOUT_EXC_NAMES = frozenset(
    {"TimeoutError", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout"}
)


def _as_status(value: Any) -> int | None:
    """An int HTTP status out of an int, a ``RouterStatus`` (an int subclass whose
    value is the INNER code), or a receipt's string token (``"402"``,
    ``"200/402"``). ``None`` when there is no number to read."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str):
        tail = value.strip().rsplit("/", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return None


def classify_status(status: Any) -> str:
    """The failure class of one HTTP status (int, RouterStatus or token)."""
    if isinstance(status, str) and status.strip() == "timeout":
        return CLASS_TIMEOUT
    if isinstance(status, str) and status.strip() == "empty":
        return CLASS_EMPTY
    n = _as_status(status)
    if n is None:
        return CLASS_ERROR
    if n in (401, 403):
        return CLASS_AUTH
    if n == 402:
        return CLASS_CREDIT
    if n == 404:
        return CLASS_RETIRED
    if n == 429:
        return CLASS_RATE_LIMITED
    if n >= 500:
        return CLASS_SERVER
    return CLASS_ERROR


def says_insufficient_credits(*texts: Any) -> bool:
    """True when any of ``texts`` carries the provider's out-of-credit wording."""
    return any(
        isinstance(t, str) and bool(_INSUFFICIENT_CREDITS_RE.search(t)) for t in texts
    )


def classify_exception(exc: BaseException) -> tuple[str, int | None]:
    """``(failure class, http status or None)`` for one raised LLM call.

    The status wins when it is route-gone or retryable; a body that names an
    empty balance upgrades any OTHER 4xx (or a status-less hard failure) to
    ``credit``, because the incident's whole lesson is that "out of money" must
    not read as "bad request". A timeout-class exception with no status is a
    timeout, never a guess.
    """
    status = _as_status(getattr(exc, "status", None))
    cls = classify_status(status) if status is not None else CLASS_ERROR
    if cls not in ROUTE_GONE_CLASSES and cls not in (CLASS_RATE_LIMITED, CLASS_SERVER):
        if says_insufficient_credits(str(exc), getattr(exc, "body", "")):
            return CLASS_CREDIT, status
    if status is None and type(exc).__name__ in _TIMEOUT_EXC_NAMES:
        return CLASS_TIMEOUT, None
    if status is None and type(exc).__name__ == "TransientLLMFailure":
        return CLASS_TIMEOUT, None  # a network error that never got a status
    return cls, status


def is_route_gone(cls: str) -> bool:
    """Does this class move a fallback chain / page at once?"""
    return cls in ROUTE_GONE_CLASSES


__all__ = [
    "CLASS_AUTH",
    "CLASS_CREDIT",
    "CLASS_EMPTY",
    "CLASS_ERROR",
    "CLASS_RATE_LIMITED",
    "CLASS_RETIRED",
    "CLASS_SERVER",
    "CLASS_TIMEOUT",
    "CLASS_UNRESOLVABLE",
    "ROUTE_GONE_CLASSES",
    "ROUTE_GONE_STATUSES",
    "classify_exception",
    "classify_status",
    "is_route_gone",
    "says_insufficient_credits",
]
