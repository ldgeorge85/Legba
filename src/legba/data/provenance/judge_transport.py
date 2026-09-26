# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Judge TRANSPORT — the retry shim and the partition-preserve policy.

K-1 brick #6 of the judge subsystem (after ``judge_verdict_parsing``,
``judge_quote_rules``, ``citation_markers``, ``absence_slice`` and
``judge_pipeline_version``). ``verify`` imports it ONE WAY and re-exports the
names it used to own, so ``verify._judge_claim_partition`` resolves exactly as
before.

WHY THIS MODULE EXISTS — the 2026-09-07 outage, measured
================================================================

The verify judge runs on the $0 core plane through OpenRouter
(``llm.judge.openrouter_nemotron120b.openai_compat`` →
``nvidia/nemotron-3-super-120b-a12b:free``, 120 s timeout). From 2026-09-07
04:00Z the fleet started grading roughly half its findings on the deterministic
floor alone. Fleet ``Faithfulness verify`` rows: 09-05 446 judged / 19
``judge_empty``; 09-07 367 / 198; 09-08 (to 19:00Z) 117 / 222.

The cause is NOT that the judge said nothing. It is that **OpenRouter reports an
upstream provider failure as HTTP 200 carrying an error envelope**::

    HTTP/1.1 200 OK
    {"id": "...", "error": {"message": "Upstream error from Nvidia: Service
     temporarily overloaded", "code": 502}}

— no ``choices`` key, no ``provider`` key. Probed live 2026-09-08, 8 requests,
2 came back in exactly that shape (25%); the receipts agree (599 of 2,019
nemotron calls since 09-07, 29.7%).

Two layers then lost the signal, and neither of them is the model:

  * ``stack/llm/base._call_chat`` DOES retry — ``{429, 500, 502, 503, 529}``
    plus ``ConnectError`` / ``ReadTimeout`` / ``RemoteProtocolError``, 3 retries,
    ``2 ** attempt`` backoff, honouring ``Retry-After``. It never fired for this
    class, because it switches on ``response.status_code`` and the status code
    was **200**. The 502 was in the BODY.
  * ``stack/llm/vllm._parse_response`` maps an empty ``choices`` list to
    ``LLMResponse(content="", finish_reason="error")``. The judge parse then
    finds no ``verdicts`` object and returns ``[]``, which
    ``verify._maybe_llm_judge`` labels ``judge_empty`` — the reason reserved for
    "the judge answered and its answer had no verdicts in it". The receipt is
    worse: ``_account_call`` records ``status="success"`` with no
    ``http_status``, so the ONE surface that could have paged on a transport
    outage recorded 599 successful calls.

So the platform could not tell "the grader was overloaded" from "the grader had
nothing to say", on the exact axis its own honesty contract turns on.

WHAT THIS MODULE DOES — and what it deliberately does not
================================================================

TRANSPORT ONLY. The judge MODEL, the judge ROUTE, the judge PROMPTS, the verdict
vocabulary, the severity chain and the scoring arithmetic are all untouched.

  1. :func:`call_judge_with_retry` wraps ONE judge call in a bounded retry:
     3 attempts, exponential backoff 2 s → 8 s with ±25% jitter, under a
     wall-clock budget (:data:`JUDGE_RETRY_BUDGET_S`, default 45 s of ADDED
     sleep per call) so a run can never be extended without bound. It retries an
     in-body 5xx/429 envelope, a real 5xx/429, a network timeout and an empty
     body; it re-raises a non-retryable hard failure immediately, preserving
     today's ``judge_error`` labelling exactly.
  2. It records what actually happened: :class:`JudgeTransportTelemetry` carries
     ``judge_attempts`` and ``judge_http_statuses`` onto the verify row, where
     ``"200/502"`` reads as *the router answered 200 and named a 502 inside*.
     That is the receipt whose absence hid this for two days.
  3. :func:`resolve_partition_outcome` is the PARTITION-PRESERVE policy — see
     its docstring for the ceiling rule, which is the graded-behaviour change
     the ``2026-09-08/1`` stamp exists for.
  4. 2026-09-20/1 extends the same argument one level down, from the partition
     to the individual verdict: a response that drops a verdict out of
     twenty-odd used to discard the whole row (``_JudgeVerdictError`` →
     ``judge_error`` → the floor at the 0.85 provisional ceiling). It is now
     salvaged when — and ONLY when — the response NAMES the claims it graded,
     so the ungraded ones can be left unchecked instead of guessed at by
     position. THE LINE THIS FILE DRAWS HOLDS: reading a response is transport,
     and this changes no prompt, no rubric, no verdict vocabulary and no
     arithmetic. It is stamped for the same reason the partition repair was —
     it MOVES VERDICTS, by moving rows out of the floor and into adjudication.

NO $0 FALLBACK EXISTS FOR THIS MODEL (checked 2026-09-08, live). OpenRouter's
``/models/nvidia/nemotron-3-super-120b-a12b:free/endpoints`` returns exactly ONE
endpoint — ``Nvidia``, prompt 0 / completion 0. There is no second free provider
slot, so ``provider.order`` / ``provider.allow_fallbacks`` in the request body
has nowhere to route and is not set: it would be ceremony, not a fallback. The
NON-free slug ``nvidia/nemotron-3-super-120b-a12b`` does carry two more
providers (DeepInfra, DigitalOcean) but both are PAID, and the core plane is $0
by standing rule — so that is a route decision for an operator, not a transport
fix, and it is not made here.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from .judge_absence_rubric import JUDGE_VERDICT_TOKENS
from .judge_verdict_parsing import (
    _extract_json_objects,
    _JudgeVerdictError,
    _VERDICT_UNCHECKED,
    align_verdicts,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Tunables
# ---------------------------------------------------------------------------

#: Total attempts for one judge call (the first try plus 2 retries).
JUDGE_RETRY_ATTEMPTS = 3
#: First backoff, seconds. Doubles per retry, capped at :data:`JUDGE_RETRY_CAP_S`.
JUDGE_RETRY_BASE_S = 2.0
#: Backoff ceiling, seconds. 2 s → 4 s → 8 s.
JUDGE_RETRY_CAP_S = 8.0
#: Fractional jitter applied to every sleep (±25%), so a fleet of analysts that
#: all hit an overloaded provider in the same minute does not re-converge on it.
JUDGE_RETRY_JITTER = 0.25
#: The ADDED wall-clock budget for one call's retries. A run's overall budget is
#: the caller's (``ctx.budget``); this is the local guarantee that the shim can
#: extend a single judge call by at most this many seconds of sleep.
JUDGE_RETRY_BUDGET_S = 45.0
#: The longest ``Retry-After`` this shim will honour, seconds. A provider asking
#: for more than this is asking for a later run, not a longer sleep.
RETRY_AFTER_CAP_S = 30.0

#: Matched to the main model's budget: a reasoning-class model may emit thinking
#: before the strict-JSON verdicts, so a 512 cap would truncate the JSON → empty
#: parse → soft-fail to floor. (Moved here with the call it belongs to; the value
#: is byte-identical to the one ``_judge_claim_partition`` passed before.)
JUDGE_MAX_TOKENS = 16384

#: ``FaithfulnessReport.judge_status`` when SOME of a finding's graded claims
#: carry an LLM verdict and the rest do not — a partition that came back empty
#: (2026-09-08/1) or a partition whose response dropped verdicts
#: (2026-09-20/1). See :func:`resolve_partition_outcome`.
JUDGE_STATUS_PARTIAL = "partial"

#: ``judge_unavailable_reason`` prefix when a partition WAS graded but its
#: response carried fewer verdicts than claims (2026-09-20/1) — rendered
#: ``judge_partial_verdicts:<partition>:<k>``. Deliberately NOT folded into
#: ``judge_empty_partition``: an empty partition and a short answer are two
#: different provider behaviours and the row must be able to say which.
JUDGE_PARTIAL_VERDICTS = "judge_partial_verdicts"

#: The single-partition name the M14 whole-finding SURVEY route reports under
#: (it has no claim-kind partition of its own — one rubric grades everything).
JUDGE_SURVEY_PARTITION = "survey"

#: ``http_statuses`` token for a clean, content-bearing response.
STATUS_OK = "200"
#: HTTP 200, well-formed, but no content at all (the judge said nothing).
STATUS_EMPTY = "empty"
#: A network-level failure with no HTTP status to name.
STATUS_TIMEOUT = "timeout"


# ---------------------------------------------------------------------------
# 2. Telemetry
# ---------------------------------------------------------------------------


@dataclass
class JudgeTransportTelemetry:
    """What the transport actually did, per verify pass.

    Accumulates ACROSS every partition of one finding — the M14 survey call, or
    the shared + absence partition pair — because that is the grain the verify
    row is written at. ``attempts`` counts requests actually sent (so a clean
    single-shot pass records ``1``); ``http_statuses`` is one token per attempt
    in order.
    """

    attempts: int = 0
    http_statuses: list[str] = field(default_factory=list)
    #: 2026-09-20/1 — the 1-based ORIGINAL claim numbers (span order, across
    #: every partition) the judge returned no verdict for. Empty on every
    #: complete pass, which is the overwhelming majority.
    unchecked_claims: list[int] = field(default_factory=list)
    #: The same gap counted per partition, for the ``judge_unavailable_reason``.
    unchecked_by_partition: dict[str, int] = field(default_factory=dict)
    #: H3 (2026-09-25/1) — the RAW reply-count mismatch: summed, across every
    #: partition this pass sent, ``(verdicts the judge returned) - (claims sent)``.
    #: Recorded the moment a partition's response parses as a verdict LIST — even
    #: when ``align_verdicts`` goes on to raise (a labelled response can still
    #: overrun its budget, or an unlabelled one can mismatch outright) — so this
    #: receipt exists on the ``judge_error`` path exactly as it does on a salvaged
    #: one. Typically 0; negative on the dominant undercount shape H3 salvages.
    reply_count_delta: int = 0

    def record(self, status: str) -> None:
        self.attempts += 1
        self.http_statuses.append(status)

    def record_unchecked(self, partition: str, claim_numbers: list[int]) -> None:
        """Note the claims one partition's response left ungraded (sparse)."""
        if not claim_numbers:
            return
        self.unchecked_claims.extend(claim_numbers)
        self.unchecked_by_partition[partition] = self.unchecked_by_partition.get(
            partition, 0
        ) + len(claim_numbers)

    def record_reply_length(self, raw_count: int, claim_count: int) -> None:
        """Note one partition's raw verdict count against its claim count (H3)."""
        self.reply_count_delta += raw_count - claim_count

    def stamp(self, report: Any) -> None:
        """Copy the receipts onto a ``FaithfulnessReport`` (no-op when unused)."""
        if not self.attempts:
            return
        report.judge_attempts = self.attempts
        report.judge_http_statuses = list(self.http_statuses)
        if self.unchecked_claims:
            report.judge_partial = len(self.unchecked_claims)
            report.judge_partial_claims = sorted(self.unchecked_claims)
        report.judge_miscount_claims = self.reply_count_delta


def judge_transport_receipts(report: Any) -> dict[str, Any]:
    """The verification-block fragment for a report's transport receipts.

    PRESENT IFF THE JUDGE WAS ASKED, which is the only rule that makes the
    receipt readable. ``judge_attempts`` is ``None`` on a report where no call
    was ever made — flag off, no handler, unsampled, no gradeable claim, or a
    directly-constructed report — and those rows stay byte-identical to the
    pre-2026-09-08 shape. It is an INT the moment a call is attempted, and the
    fragment then always ships, ``1`` included.

    The distinction is the whole point. If the key were omitted whenever it read
    ``1``, an adjudicated row that was judged on the first try would be
    indistinguishable from one whose receipt was LOST — and losing it is exactly
    what happened between 2026-09-08 20:53Z and 21:02Z, when three live
    ``judge_status='llm'`` rows shipped with no receipt at all because the
    persisted block is hand-written and never consulted this function. An
    ``int`` that reads ``0`` on an adjudicated row is a visible contradiction;
    an absent key is a silent one, and reads as "no judge ran".
    """
    attempts = getattr(report, "judge_attempts", None)
    if attempts is None:
        return {}
    out: dict[str, Any] = {
        "judge_attempts": int(attempts),
        "judge_http_statuses": list(getattr(report, "judge_http_statuses", []) or []),
    }
    # 2026-09-20/1 — the PARTIAL receipt, and it is sparse on a different rule
    # from the pair above: ``judge_partial`` is absent unless the judge actually
    # left a claim ungraded, so every complete pass (the overwhelming majority)
    # keeps a byte-identical verification block. ``judge_partial_claims`` names
    # WHICH claims, in span order, so the gap is auditable from the row instead
    # of only from a log line.
    partial = getattr(report, "judge_partial", None)
    if partial:
        out["judge_partial"] = int(partial)
        out["judge_partial_claims"] = list(
            getattr(report, "judge_partial_claims", []) or []
        )
    return out


# ---------------------------------------------------------------------------
# 3. Classifying one response
# ---------------------------------------------------------------------------


def envelope_error(response: Any) -> Mapping[str, Any] | None:
    """The in-body error envelope an OpenRouter-style router returns on HTTP 200.

    ``LLMResponse.raw_response`` is the parsed provider JSON verbatim, so this
    reads the shape the live probe recorded: ``{"error": {"message": ..,
    "code": 502}}`` with no ``choices``. ``None`` when the body carries no such
    envelope, which is every healthy response and every non-routed provider.
    """
    raw = getattr(response, "raw_response", None)
    if not isinstance(raw, Mapping):
        return None
    err = raw.get("error")
    return err if isinstance(err, Mapping) else None


def _envelope_code(err: Mapping[str, Any]) -> int | None:
    for key in ("code", "status", "status_code"):
        try:
            code = int(err[key])  # type: ignore[index]
        except (KeyError, TypeError, ValueError):
            continue
        if 100 <= code < 600:
            return code
    return None


def _envelope_retry_after(err: Mapping[str, Any]) -> float | None:
    meta = err.get("metadata")
    headers = meta.get("headers") if isinstance(meta, Mapping) else None
    if not isinstance(headers, Mapping):
        return None
    for key in ("Retry-After", "retry-after"):
        try:
            return float(headers[key])  # type: ignore[index]
        except (KeyError, TypeError, ValueError):
            continue
    return None


def classify_response(response: Any) -> tuple[str, str, float | None]:
    """``(status token, content, retry_after)`` for one judge response.

    Three outcomes, and the middle one is the whole point of this module:

    * content present            → :data:`STATUS_OK`
    * in-body error envelope     → ``"200/<code>"`` (e.g. ``"200/502"``) — the
      router answered 200 and named an upstream failure INSIDE the body. This is
      a TRANSPORT failure wearing a success status code.
    * empty content, no envelope → :data:`STATUS_EMPTY`, the honest
      "the judge answered and said nothing" this platform already had a name for.
    """
    content = getattr(response, "content", "") or ""
    err = envelope_error(response)
    if err is not None:
        code = _envelope_code(err)
        token = f"200/{code}" if code is not None else "200/error"
        return token, "", _envelope_retry_after(err)
    if not content.strip():
        return STATUS_EMPTY, "", None
    return STATUS_OK, content, None


def status_is_retryable(token: str) -> bool:
    """Is a recorded status token worth another attempt?

    Retryable: an empty body, a network timeout, 429, and any 5xx — whether it
    arrived as an HTTP status or inside a 200's error envelope. NOT retryable:
    every other 4xx (auth, policy, a model id the router does not serve), which
    is a configuration verdict that a second request cannot change.
    """
    if token in (STATUS_EMPTY, STATUS_TIMEOUT):
        return True
    if token == STATUS_ERROR:
        return False  # not a transport failure at all — see _exception_status
    code = token.rsplit("/", 1)[-1]
    try:
        n = int(code)
    except ValueError:
        return True  # "200/error" — an unnamed upstream failure; try again
    return n == 429 or n >= 500


#: Exception CLASS NAMES this shim will consider retrying. Anything else — a
#: ``BudgetExhausted``, a programming error, a test double's bare ``RuntimeError``
#: — is recorded and re-raised on the FIRST attempt: burning two backoff sleeps
#: on a budget that is already spent delays an honest ``judge_error`` and buys
#: nothing. Keyed by name, not by class, so a re-declared double still buckets.
_TRANSPORT_EXC_NAMES: frozenset[str] = frozenset(
    {
        "TransientLLMFailure",
        "HardLLMFailure",
        "TimeoutError",
        "ReadTimeout",
        "ConnectError",
        "ConnectTimeout",
        "RemoteProtocolError",
        "OSError",
    }
)

#: A transport exception carrying no HTTP status at all — a socket that never
#: answered. Recorded as such rather than guessed at.
STATUS_ERROR = "error"


def _exception_status(exc: BaseException) -> str:
    """``(status token)`` for a raised call, and whether it is even transport.

    A recognised transport failure yields its HTTP status, or
    :data:`STATUS_TIMEOUT` when it never got one. Everything else yields
    :data:`STATUS_ERROR`, which :func:`status_is_retryable` refuses — so the
    exception propagates on the first attempt and the row reads ``judge_error``,
    exactly as it does today.
    """
    if type(exc).__name__ not in _TRANSPORT_EXC_NAMES:
        return STATUS_ERROR
    status = getattr(exc, "status", None)
    return str(status) if isinstance(status, int) else STATUS_TIMEOUT


def backoff_delay(attempt: int, *, retry_after: float | None = None) -> float:
    """Seconds to sleep before attempt ``attempt`` (0-based, so the FIRST retry
    is ``attempt=0`` → ~2 s, then ~4 s, then capped at 8 s), jittered ±25%.

    A provider-supplied ``Retry-After`` WINS over the schedule (it is the only
    party that knows when it will be ready), capped at
    :data:`RETRY_AFTER_CAP_S` and never jittered — an explicit instruction is
    not a guess to be spread out.
    """
    if retry_after is not None and retry_after > 0:
        return min(float(retry_after), RETRY_AFTER_CAP_S)
    base = min(JUDGE_RETRY_BASE_S * (2**attempt), JUDGE_RETRY_CAP_S)
    return base * (1.0 + random.uniform(-JUDGE_RETRY_JITTER, JUDGE_RETRY_JITTER))


# ---------------------------------------------------------------------------
# 4. The retry loop
# ---------------------------------------------------------------------------


async def call_judge_with_retry(
    judge_llm: Any,
    *,
    evidence_prompt: str,
    system: str,
    telemetry: JudgeTransportTelemetry,
    max_tokens: int = JUDGE_MAX_TOKENS,
    temperature: float = 0.0,
    attempts: int = JUDGE_RETRY_ATTEMPTS,
    budget_s: float = JUDGE_RETRY_BUDGET_S,
    sleeper: Callable[[float], Awaitable[None]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Send ONE judge prompt, retrying transport failures; return the content.

    Returns ``""`` when every attempt came back empty or upstream-failed — the
    caller then soft-fails that partition to the floor exactly as it did before,
    so the ``judge_empty`` reason keeps its meaning. RAISES the provider's own
    exception when the failure is hard and non-retryable, or when a retryable
    exception outlives the attempt budget, so ``judge_error`` also keeps its
    meaning (the 09-06 19:00–23:59Z 429 storm was 141 calls and it is correctly
    ``judge_error`` today; nothing here reclassifies it).

    The budget is a floor on honesty, not a ceiling on effort: it bounds the
    SLEEP this shim adds, never the provider's own 120 s per-request timeout,
    and it is checked BEFORE sleeping so an exhausted budget stops immediately
    rather than after one more long wait.
    """
    # Resolved at CALL time, not bound as a default: a default would freeze
    # ``asyncio.sleep`` at import and make the backoff untestable without
    # actually waiting for it.
    sleep = sleeper if sleeper is not None else asyncio.sleep
    started = clock()
    last_exc: BaseException | None = None
    for attempt in range(max(1, attempts)):
        retry_after: float | None = None
        try:
            response = await judge_llm.chat_complete(
                [{"role": "user", "content": evidence_prompt}],
                # The faithfulness judge runs on the SAME core reasoning model as
                # generation (2026-07-01: the 8B cross-family judge proved too
                # weak — harsh + mis-aimed). Same-family removes cross-family
                # independence — a DOCUMENTED LIMITATION; the deterministic
                # citation floor + the provenance chain still backstop it.
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
            )
        except BaseException as exc:  # noqa: BLE001 — classified, then re-raised
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                raise
            token = _exception_status(exc)
            telemetry.record(token)
            last_exc = exc
            retry_after = getattr(exc, "retry_after", None)
            if not status_is_retryable(token):
                raise
        else:
            token, content, retry_after = classify_response(response)
            telemetry.record(token)
            last_exc = None
            if token == STATUS_OK:
                return content
            if not status_is_retryable(token):
                return ""

        if attempt + 1 >= max(1, attempts):
            break
        delay = backoff_delay(attempt, retry_after=retry_after)
        if (clock() - started) + delay > budget_s:
            logger.warning(
                "verify.judge.transport.budget_exhausted attempts=%d statuses=%s "
                "— the retry budget (%.0fs) would be exceeded by a %.1fs sleep; "
                "this partition soft-fails to the deterministic floor",
                telemetry.attempts, telemetry.http_statuses, budget_s, delay,
            )
            break
        logger.info(
            "verify.judge.transport.retry attempt=%d status=%s delay=%.1fs "
            "— the judge route failed in TRANSPORT, not in judgement",
            attempt + 1, telemetry.http_statuses[-1], delay,
        )
        await sleep(delay)

    if last_exc is not None:
        raise last_exc
    logger.warning(
        "verify.judge.transport.exhausted attempts=%d statuses=%s — every "
        "attempt came back empty or upstream-failed; this partition falls to "
        "the deterministic floor",
        telemetry.attempts, telemetry.http_statuses,
    )
    return ""


# ---------------------------------------------------------------------------
# 5. PARTITION-PRESERVE — the graded-behaviour change behind 2026-09-08/1
# ---------------------------------------------------------------------------


def resolve_partition_outcome(
    *,
    judged: bool,
    floored: list[str],
    unchecked: Mapping[str, int] | None = None,
) -> tuple[str, str | None]:
    """``(judge_status, judge_unavailable_reason)`` for one verify pass.

    ``_run_judge`` splits a finding's claims into an ABSENCE partition and a
    shared (``citation_support``) partition and makes ONE judge call for each.
    Before this train, EITHER partition coming back empty discarded BOTH — two
    ``return [], {}`` statements — so at a ~30% per-request failure rate a
    finding carrying a single absence claim lost its verdicts about half the
    time, and lost the verdicts the judge HAD produced along with the ones it
    had not.

    Now each partition stands or falls on its own:

    * **every partition judged** → ``('llm', None)``, byte-identical to before.
    * **some judged, some empty** → ``('partial', 'judge_empty_partition:<names>')``.
      The judged partition's verdicts are kept and are authoritative over the
      prose they graded; the empty partition's claims are simply not in
      ``verdicts``, so the existing reconciliation already treats them as claims
      the judge could not grade — their floor spans fold back in through
      ``residual_floor_spans`` (counted as unsupported, in the denominator) and
      their floor ledger rows carry over through ``carried_ledger``. No verdict
      is fabricated for a claim nobody graded.
    * **no partition judged** → ``('deterministic', None)`` and the caller's
      existing ``judge_empty`` labelling stands, unchanged.

    THE CEILING RULE, written down because it is the point
    -----------------------------------------------------
    ``PROVISIONAL_SCORE_CEILING`` (0.85) is the mark of a verdict that NO grader
    adjudicated — Q-1(c)'s answer to a 26-hour outage that published 611 scored
    critiques with nothing saying the grader was gone. It therefore applies
    **iff the report carries zero judged verdicts**:

      * ``deterministic`` / ``unsampled`` → provisional → the 0.85 cap applies,
        exactly as before this train.
      * ``partial`` → NOT provisional → NO 0.85 cap. A grader did adjudicate the
        claims that survive in the denominator, and the floored partition is
        already CHARGED for rather than excused: its unsupported floor spans
        enter ``effective_checkable`` as failures, so a partial pass can only
        score at or below what a fully-graded pass would. Capping it again would
        charge the finding twice for one provider's overload.
      * ``llm`` → NOT provisional → no cap, unchanged.

    ``is_provisional`` is the single function that decides this
    (``judge_assessability``), so the rule is enforced in one place and
    ``gate_score`` needs no change at all.

    2026-09-20/1 adds the SECOND way a pass can be incomplete: ``unchecked``
    maps a partition that WAS graded to the number of its claims whose verdict
    the judge dropped (``judge_verdict_parsing.align_verdicts``). It lands in
    exactly the same state on exactly the same reasoning — a grader adjudicated
    the claims that survive, the unchecked ones are charged for by the floor
    rather than excused, so ``partial`` and no 0.85 cap — and it is reported
    under its OWN prefix (``judge_partial_verdicts:<partition>:<k>``), because a
    partition that answered nothing and a partition that answered short are two
    different provider behaviours. A pass can carry both; the reasons then ride
    one string, ``;``-joined, in that order. THE POPULATION THIS REACHES IS
    NARROW BY CONSTRUCTION: every row it touches is one that, before this
    train, raised ``_JudgeVerdictError`` and published the deterministic floor
    under ``judge_error`` — so no row can move from ``llm`` to ``partial``.
    """
    if not judged:
        return "deterministic", None
    reasons: list[str] = []
    if floored:
        reasons.append(f"judge_empty_partition:{','.join(sorted(floored))}")
    if unchecked:
        reasons.append(
            f"{JUDGE_PARTIAL_VERDICTS}:"
            + ",".join(f"{name}:{n}" for name, n in sorted(unchecked.items()))
        )
    if not reasons:
        return "llm", None
    return JUDGE_STATUS_PARTIAL, ";".join(reasons)


# ---------------------------------------------------------------------------
# 6. ONE PARTITION, SENT AND PARSED
# ---------------------------------------------------------------------------
# Moved here from ``verify`` 2026-09-08 (the module-size gate's seam — the
# call and the parse of one judge response belong beside the transport that
# makes the call). Re-exported by ``verify``, so
# ``verify._judge_claim_partition`` resolves exactly as before and every test
# that patches it keeps working.


def split_unchecked(
    claim_idx: list[int], verdicts: list[tuple[str, str, str]]
) -> tuple[list[tuple[int, tuple[str, str, str]]], list[int]]:
    """``(graded, unchecked)`` for ONE partition's ``(verdict, quote, aligned_by)``
    list.

    ``claim_idx`` holds the partition's claims' ORIGINAL 0-based positions in
    span order. ``graded`` pairs each surviving verdict with its original index
    (ready for the severity chain); ``unchecked`` is the 1-based claim NUMBERS
    whose slot the judge left empty (2026-09-20/1) — never scored, never
    floored by fiat, and never handed to ``_severity``.

    Lives here rather than in ``verify`` because the sentinel is this module's
    output contract, and because all three partition call sites must drop it the
    SAME way — a site that forgot would score a gap as an unsupported claim.
    """
    graded: list[tuple[int, tuple[str, str, str]]] = []
    unchecked: list[int] = []
    for i, (verdict, quote, aligned_by) in zip(claim_idx, verdicts):
        if verdict == _VERDICT_UNCHECKED:
            unchecked.append(i + 1)
        else:
            graded.append((i, (verdict, quote, aligned_by)))
    return graded, unchecked


async def _judge_claim_partition(
    judge_llm: Any,
    *,
    claims: list[str],
    evidence_prompt: str,
    system: str,
    telemetry: JudgeTransportTelemetry | None = None,
) -> list[tuple[str, str, str]]:
    """Send ONE partition of claims to the judge; return
    ``[(verdict, quote, aligned_by)]``.

    Factored out of :func:`_run_judge` so the V3 absence partition AND the M14
    whole-finding survey call reuse the identical call + parse machinery with
    their OWN system prompts (design §3.5). A malformed / empty response yields
    ``[]``; a length mismatch raises :class:`_JudgeVerdictError` (the
    ONE-verdict-per-claim honesty contract from #116d). ``evidence_prompt`` is
    the per-branch user message already carrying the evidence map + numbered
    claim list.

    The returned list is ALWAYS ``len(claims)`` long when it is non-empty, and
    2026-09-20/1 is the only reason that is worth saying: a claim whose verdict
    the judge dropped, in a response that NAMED the ones it did grade, comes
    back as ``(_VERDICT_UNCHECKED, "", aligned_by)``. That is a SLOT, not a
    verdict — the caller must skip it, never score it and never hand it to the
    severity chain, exactly as it already skips a floored partition's claims.
    ``aligned_by`` (H3, 2026-09-25/1) is :data:`~judge_verdict_parsing.
    ALIGNED_BY_CLAIM_INDEX` or ``ALIGNED_BY_POSITIONAL`` — ONE value for the
    whole response (see :func:`~judge_verdict_parsing.align_verdicts`),
    repeated onto every entry (including the unchecked ones, where it is
    harmless — the caller drops them before it would ever be read).

    V-D: ``quotes`` is the OPTIONAL parallel array of verbatim evidence spans —
    ``""`` for every entry a judge omits, a wrong-length array ignored wholesale
    (a misaligned quote is worse than none). Resolution against the shown
    evidence is the CALLER's job (it holds the evidence map).
    """
    # 2026-09-08/1 — the send goes through the bounded retry above. The model,
    # the route, ``max_tokens`` and ``temperature`` are byte-identical to the
    # direct ``chat_complete`` this replaced; only the number of times an
    # unreachable judge is asked has changed. Everything below — the
    # fence-tolerant parse, the length contract, the quote array, the verdict
    # vocabulary — is the pre-move code verbatim.
    content = await call_judge_with_retry(
        judge_llm,
        evidence_prompt=evidence_prompt,
        system=system,
        telemetry=telemetry if telemetry is not None else JudgeTransportTelemetry(),
        max_tokens=JUDGE_MAX_TOKENS,
        temperature=0.0,
    )
    # (#116d) Fence/prose-tolerant parse: a reasoning-class judge may wrap the
    # verdicts in a ```json fence or emit thinking around them, so scan for the
    # object that actually carries ``verdicts`` instead of a fence-intolerant
    # ``strip('`')`` that fails on ``` ```json\n{...}\n``` ```.
    parsed = next(
        (o for o in _extract_json_objects(content) if "verdicts" in o), None
    )
    if parsed is None:
        return []  # no parseable verdict object → soft-fail to floor (judge_empty)
    raw = parsed.get("verdicts")
    if not isinstance(raw, list):
        return []
    # H3 (2026-09-25/1): the raw reply-count mismatch, recorded BEFORE
    # ``align_verdicts`` runs — it may still raise below (a labelled reply can
    # overrun ``partial_verdict_budget``, an unlabelled one can mismatch
    # outright), and the receipt must exist on that path too.
    if telemetry is not None:
        telemetry.record_reply_length(len(raw), len(claims))
    # (#116d) HONEST length contract: the judge MUST return one verdict per
    # graded claim. A short/long list previously zip-truncated to the shorter,
    # silently passing the ungraded tail — that is still refused.
    #
    # 2026-09-20/1: ``align_verdicts`` decides WHICH claim each verdict belongs
    # to before the contract is checked. A full-length positional response is
    # byte-identical to the ``zip`` this replaced. A SHORT response is salvaged
    # only when its entries NAME their claims and the gap is inside
    # ``partial_verdict_budget``; the named claims keep their verdicts and the
    # rest come back ``_VERDICT_UNCHECKED`` — a slot, not a grade, which the
    # caller drops from the judged population rather than scoring. Everything
    # else still raises and still lands as ``judge_error``.
    slots, missing, aligned_by = align_verdicts(raw, parsed, len(claims))
    if missing:
        logger.warning(
            "verify.judge.partial_verdicts returned=%d claims=%d unchecked=%s "
            "— the judge NAMED the claims it graded, so the rest are left "
            "unchecked rather than guessed at by position",
            len(raw), len(claims), [i + 1 for i in missing],
        )
    out: list[tuple[str, str, str]] = []
    for slot in slots:
        if slot is None:
            out.append((_VERDICT_UNCHECKED, "", aligned_by))
            continue
        verdict, quote = slot
        v = str(verdict).strip().lower()
        # RUST-3: the accepted vocabulary is the FOUR-token contract. Anything
        # outside it still coerces to ``unsupported``, exactly as before — the
        # only change is that ``not_a_proposition`` stopped being "anything
        # outside it". A judge on ANY route may now say a span asserts nothing;
        # whether it is honoured is the severity chain's decision, not the
        # parser's (only the absence rubric currently ADVERTISES the token).
        if v not in JUDGE_VERDICT_TOKENS:
            v = "unsupported"
        out.append((v, quote, aligned_by))
    return out


__all__ = [
    "JUDGE_MAX_TOKENS",
    "JUDGE_PARTIAL_VERDICTS",
    "JUDGE_SURVEY_PARTITION",
    "JUDGE_RETRY_ATTEMPTS",
    "JUDGE_RETRY_BASE_S",
    "JUDGE_RETRY_BUDGET_S",
    "JUDGE_RETRY_CAP_S",
    "JUDGE_RETRY_JITTER",
    "JUDGE_STATUS_PARTIAL",
    "RETRY_AFTER_CAP_S",
    "STATUS_EMPTY",
    "STATUS_ERROR",
    "STATUS_OK",
    "STATUS_TIMEOUT",
    "JudgeTransportTelemetry",
    "_judge_claim_partition",
    "backoff_delay",
    "call_judge_with_retry",
    "classify_response",
    "envelope_error",
    "judge_transport_receipts",
    "resolve_partition_outcome",
    "split_unchecked",
    "status_is_retryable",
]
