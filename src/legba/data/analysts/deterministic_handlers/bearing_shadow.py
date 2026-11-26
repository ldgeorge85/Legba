# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""the bearing gate's SHADOW (a decision model or a chat model).

WHAT IT IS
----------
The bearing gate (:mod:`.bearing_gate`) asks a small chat model, per candidate
edge, whether new evidence bears on an open question — ≈ 32,000 calls a week.
A DECISION model behind the same gateway (``POST /decide``)
answers exactly that shape of question — a typed choice with a probability per
option, in ≈ 0.3 s — and may do it better or cheaper. Before it may decide
anything it is measured: every edge the gate ASKED is also asked of the shadow,
and the pair (gate verdict, shadow choice + p_yes + confidence + latency) lands
in ``decision_shadow_ledger`` (migration 0238). ``scripts/decision_shadow_report
.py`` reads the agreement, the 2×2 confusion and the calibration out of it.

WHAT IT IS NOT
--------------
It moves NO decision. It runs after the gate, the confirm leg AND the edge
writes have finished; it reads the ``(candidate, verdict)`` pairs the gate
recorded and never touches a candidate. Its failures — a component that will not
build, a 429, a timeout, a ledger write that fails — are COUNTED on the receipt
(``bearing_shadow_*``) and logged, never raised. With ``bearing_shadow_ref``
unset (the shipped default) it does nothing at all and adds nothing to the
receipt.

THE STATE AND THE QUESTION
--------------------------
The state is :func:`.bearing_gate.gate_row_text` — byte for byte the row the
gate's last user turn carries (the optional ``Desk:`` line, the question thesis,
the news item), so the two models judge the same text. The question is ONE
choice, ``bears``, with criteria ``yes`` / ``no``. The gate's system prompt and
its eight few-shot exemplars are NOT sent: a decision model takes instructions
and criteria, not a conversation, and the measurement is of the model as it
would be deployed.

EITHER SHAPE OF SHADOW
---------------------------------
On 2026-11-05 the roles swapped: the decision model became the GATE and the chat judge
(Gemma) its shadow for a week. So the shadow, like the gate, asks whichever
shape of component it is given:

  * a client exposing ``decide`` is asked the one choice question above
    (:func:`shadow_questions` IS :func:`.bearing_gate.bearing_questions`, the
    question the gate asks a decision model) — the row carries its choice,
    ``p_yes`` and confidence;
  * any other client is asked the chat gate's own conversation and call
    (:func:`.bearing_gate.gate_llm_call` — the system prompt, the eight
    exemplars, the same row text) and its reply is read by the gate's own
    :func:`.bearing_gate.parse_gate_verdict`. The row carries the parsed
    ``yes``/``no`` as ``shadow_choice`` and NULL ``shadow_p_yes`` /
    ``shadow_confidence`` (a chat model gives neither); a reply with no
    YES/NO in it is a row with ``shadow_error='unparseable'``.

``gate_component_id`` / ``shadow_component_id`` on every row say which
component held which role, so the report never has to assume.

BOUNDS
------
``bearing_shadow_cap`` (default: the gate cap) bounds the calls per run; edges
past it are counted ``bearing_shadow_deferred`` and get no row. Calls run
:data:`SHADOW_CONCURRENCY` at a time, each under :data:`SHADOW_TIMEOUT_SECONDS`
with at most :data:`SHADOW_MAX_RETRIES` retry; once
:data:`SHADOW_BREAKER_FAILURES` calls have failed with none answered the rest of
the run is skipped (rows carry ``shadow_error='breaker_open'``), so a dead
gateway costs one batch of timeouts rather than the cap's worth.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from uuid import UUID

from ....runtime.llm_handler_cache import register_handler_cache
from .bearing_gate import (
    BEARING_CRITERIA,
    BEARING_INSTRUCTIONS,
    BEARING_QUESTION_ID,
    EdgeCandidate,
    _unit,
    bearing_questions,
    build_component_handler,
    gate_llm_call,
    gate_row_text,
    is_decide_client,
    parse_gate_verdict,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_BEARING_SHADOW_REF",
    "SHADOW_CRITERIA",
    "SHADOW_DEPS_EXTRA_KEY",
    "SHADOW_INSTRUCTIONS",
    "SHADOW_LEG",
    "SHADOW_QUESTION_ID",
    "SHADOW_UNPARSEABLE",
    "ShadowRow",
    "run_bearing_shadow",
    "shadow_counter_defaults",
    "shadow_enabled",
    "shadow_questions",
    "write_shadow_rows",
]

#: ``bearing_shadow_ref`` — OFF in code. Turning the shadow on is a
#: descriptor PUT naming a decision component (``llm.decide.gateway_typed_choice``)
#: or, since W11-F148, a chat component (``llm.verify.gateway_judge.openai_compat``).
DEFAULT_BEARING_SHADOW_REF: str | None = None

#: The ``leg`` tag on the shadow's ``analyst_traces.llm_calls`` receipts and
#: on its ledger rows.
SHADOW_LEG = "bearing_shadow"

#: ``deps.extras`` key a caller may inject a decision client under (tests, and
#: a future pre-resolved wiring) — the gate's ``SLM_DEPS_EXTRA_KEY`` idiom.
SHADOW_DEPS_EXTRA_KEY = "claim_watch_bearing_shadow_decider"

#: The question, ONE definition shared with the gate: these names
#: are the gate module's, re-exported under the shadow's original names.
SHADOW_QUESTION_ID = BEARING_QUESTION_ID
SHADOW_INSTRUCTIONS = BEARING_INSTRUCTIONS
SHADOW_CRITERIA: Mapping[str, str] = BEARING_CRITERIA

#: ``shadow_error`` for a chat reply carrying no standalone YES/NO.
SHADOW_UNPARSEABLE = "unparseable"

#: Wire bounds — module constants, not operator knobs (the gate's rule).
SHADOW_TIMEOUT_SECONDS = 30.0
SHADOW_MAX_RETRIES = 1
SHADOW_CONCURRENCY = 8
SHADOW_BREAKER_FAILURES = 5
MAX_ERROR_CHARS = 200

_SHADOW_CLIENT_CACHE: dict[str, Any] = {}
register_handler_cache("claim_watch.bearing_shadow", _SHADOW_CLIENT_CACHE)


def shadow_enabled(ref: Any) -> bool:
    """On iff a non-empty component ref is set."""
    return bool(str(ref or "").strip())


#: The one choice question the shadow asks a decision component, in the
#: /decide shape — the gate's own function, not a copy.
shadow_questions = bearing_questions


def shadow_counter_defaults(ref: str) -> dict[str, Any]:
    """Receipt counters, seeded only when the shadow is ON (an off run's
    receipt is byte-identical to a build without the shadow)."""
    return {
        "bearing_shadow_ref": ref,
        "bearing_shadow_calls": 0,
        "bearing_shadow_answered": 0,
        "bearing_shadow_errors": 0,
        "bearing_shadow_deferred": 0,
        "bearing_shadow_skipped": 0,
        "bearing_shadow_agree": 0,
        "bearing_shadow_rows": 0,
        "bearing_shadow_write_errors": 0,
    }


@dataclass
class ShadowRow:
    """One ``decision_shadow_ledger`` row (the run-level ids are added at
    write time)."""

    target_id: str
    question_id: Any
    edge_ref: str
    gate_component_id: str
    gate_verdict: str
    shadow_component_id: str
    shadow_choice: str | None
    shadow_p_yes: float | None
    shadow_confidence: float | None
    shadow_latency_ms: int | None
    shadow_error: str | None
    state_sha256: str


def _edge_ref(cand: EdgeCandidate) -> str:
    return f"signal:{cand.signal_id}->question:{cand.question_id}"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Unparseable(Exception):
    """A chat shadow's reply carried no standalone YES/NO."""


def _error_label(exc: BaseException) -> str:
    """Class + status, never the message — a provider body can echo the
    request, and the ledger is not where a prompt should leak to."""
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    if isinstance(exc, _Unparseable):
        return SHADOW_UNPARSEABLE
    status = getattr(exc, "status", None)
    label = type(exc).__name__
    if isinstance(status, int):
        label = f"{label}:{int(status)}"
    return label[:MAX_ERROR_CHARS]


async def _resolve_client(deps: Any, ref: str) -> Any:
    extras = getattr(deps, "extras", None) if deps is not None else None
    if isinstance(extras, Mapping) and extras.get(SHADOW_DEPS_EXTRA_KEY) is not None:
        return extras[SHADOW_DEPS_EXTRA_KEY]
    cached = _SHADOW_CLIENT_CACHE.get(ref)
    if cached is not None:
        return cached
    handler = await build_component_handler(ref)
    _SHADOW_CLIENT_CACHE[ref] = handler
    logger.info("claim_watch.bearing_shadow.client_built ref=%s", ref)
    return handler


async def _ask_all(
    pairs: Sequence[tuple[EdgeCandidate, str]],
    *,
    client: Any,
    shadow_ref: str,
    gate_ref: str,
    counters: dict[str, Any],
) -> list[ShadowRow]:
    questions = shadow_questions()
    decide = is_decide_client(client)
    sem = asyncio.Semaphore(SHADOW_CONCURRENCY)
    tally = {"ok": 0, "failed": 0}

    async def _ask(cand: EdgeCandidate, state: str) -> tuple[str, Any]:
        """``(choice, answer)``; ``answer`` is the /decide answer mapping or
        ``None`` for a chat shadow. RAISES on any failure (the caller
        labels it)."""
        if decide:
            answers = await asyncio.wait_for(
                client.decide(
                    state,
                    questions,
                    leg=SHADOW_LEG,
                    max_retries=SHADOW_MAX_RETRIES,
                ),
                timeout=SHADOW_TIMEOUT_SECONDS,
            )
            answer = answers[SHADOW_QUESTION_ID]
            return str(answer["choice"]), answer
        # A CHAT shadow: the gate's own conversation and call.
        response = await asyncio.wait_for(
            gate_llm_call(client, cand), timeout=SHADOW_TIMEOUT_SECONDS
        )
        verdict = parse_gate_verdict(getattr(response, "content", "") or "")
        if verdict is None:
            raise _Unparseable()
        return verdict, None

    async def _one(cand: EdgeCandidate, verdict: str) -> ShadowRow:
        state = gate_row_text(cand)
        row = ShadowRow(
            target_id=cand.target_id,
            question_id=cand.question_id,
            edge_ref=_edge_ref(cand),
            gate_component_id=gate_ref,
            gate_verdict=verdict,
            shadow_component_id=shadow_ref,
            shadow_choice=None,
            shadow_p_yes=None,
            shadow_confidence=None,
            shadow_latency_ms=None,
            shadow_error=None,
            state_sha256=_sha(state),
        )
        async with sem:
            if tally["failed"] >= SHADOW_BREAKER_FAILURES and tally["ok"] == 0:
                row.shadow_error = "breaker_open"
                counters["bearing_shadow_skipped"] += 1
                return row
            counters["bearing_shadow_calls"] += 1
            started = time.monotonic()
            try:
                choice, answer = await _ask(cand, state)
            except Exception as exc:  # noqa: BLE001 — counted, never raised
                row.shadow_latency_ms = int((time.monotonic() - started) * 1000)
                row.shadow_error = _error_label(exc)
                tally["failed"] += 1
                counters["bearing_shadow_errors"] += 1
                return row
        row.shadow_latency_ms = int((time.monotonic() - started) * 1000)
        row.shadow_choice = choice
        if answer is not None:
            probs = answer.get("probabilities") or {}
            p_yes = probs.get("yes")
            row.shadow_p_yes = _unit(p_yes)
            row.shadow_confidence = _unit(answer.get("confidence"))
        tally["ok"] += 1
        counters["bearing_shadow_answered"] += 1
        if verdict in ("yes", "no") and choice == verdict:
            counters["bearing_shadow_agree"] += 1
        return row

    return list(await asyncio.gather(*(_one(c, v) for c, v in pairs)))


_INSERT_SQL = """
INSERT INTO decision_shadow_ledger (
    run_id, analyst_id, target_id, question_id, edge_ref, leg,
    gate_component_id, gate_verdict, shadow_component_id, shadow_choice,
    shadow_p_yes, shadow_confidence, shadow_latency_ms, shadow_error,
    state_sha256
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
"""


def _uuid(raw: Any) -> UUID | None:
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw)) if raw else None
    except (TypeError, ValueError):
        return None


async def write_shadow_rows(
    pool: Any,
    rows: Sequence[ShadowRow],
    *,
    run_id: Any,
    analyst_id: Any,
    target_id: Any,
    counters: dict[str, Any],
) -> None:
    """Insert the rows in one statement batch. Never raises: a failed write is
    ``bearing_shadow_write_errors`` += the rows lost, and a warning."""
    if not rows:
        return
    run_uuid = _uuid(run_id)
    run_target = str(target_id or "") or None
    args = [
        (
            run_uuid,
            str(analyst_id) if analyst_id else None,
            r.target_id or run_target,
            _uuid(r.question_id),
            r.edge_ref,
            SHADOW_LEG,
            r.gate_component_id,
            r.gate_verdict,
            r.shadow_component_id,
            r.shadow_choice,
            r.shadow_p_yes,
            r.shadow_confidence,
            r.shadow_latency_ms,
            r.shadow_error,
            r.state_sha256,
        )
        for r in rows
    ]
    try:
        async with pool.acquire() as conn:
            await conn.executemany(_INSERT_SQL, args)
    except Exception as exc:  # noqa: BLE001 — the shadow never fails the run
        counters["bearing_shadow_write_errors"] += len(rows)
        logger.warning(
            "claim_watch.bearing_shadow.write_failed rows=%d err=%s — the "
            "gate's edges are already written; only the measurement is lost",
            len(rows),
            type(exc).__name__,
        )
        return
    counters["bearing_shadow_rows"] += len(rows)


async def run_bearing_shadow(
    asked: Sequence[tuple[EdgeCandidate, str]],
    *,
    deps: Any,
    pool: Any,
    shadow_ref: Any,
    shadow_cap: int,
    gate_ref: str,
    run_id: Any,
    analyst_id: Any,
    target_id: Any,
    counters: dict[str, Any],
) -> list[ShadowRow]:
    """Shadow every gate-asked pair (up to the cap) and ledger the results.

    OFF (``shadow_ref`` unset) returns ``[]`` at once and touches nothing —
    not even the receipt. ON, it seeds the ``bearing_shadow_*`` counters and
    NEVER raises."""
    if not shadow_enabled(shadow_ref):
        return []
    ref = str(shadow_ref).strip()
    counters.update(shadow_counter_defaults(ref))
    try:
        cap = max(0, int(shadow_cap))
        budgeted = list(asked)[:cap]
        counters["bearing_shadow_deferred"] = max(0, len(asked) - len(budgeted))
        if not budgeted:
            return []
        try:
            client = await _resolve_client(deps, ref)
            # A decision OR a chat component; the shape is the
            # branch in ``_ask_all``. Only a client with neither surface is
            # refused.
            if not is_decide_client(client) and not callable(
                getattr(client, "chat_complete", None)
            ):
                raise TypeError(
                    f"{ref} answers neither decide() nor chat_complete()"
                )
        except Exception as exc:  # noqa: BLE001 — one row per edge, labelled
            logger.warning(
                "claim_watch.bearing_shadow.client_unavailable ref=%s err=%s — "
                "every shadowed edge this run is ledgered with the error; the "
                "gate is untouched",
                ref,
                exc,
            )
            label = f"client_unavailable:{type(exc).__name__}"[:MAX_ERROR_CHARS]
            rows = [
                ShadowRow(
                    target_id=c.target_id,
                    question_id=c.question_id,
                    edge_ref=_edge_ref(c),
                    gate_component_id=gate_ref,
                    gate_verdict=v,
                    shadow_component_id=ref,
                    shadow_choice=None,
                    shadow_p_yes=None,
                    shadow_confidence=None,
                    shadow_latency_ms=None,
                    shadow_error=label,
                    state_sha256=_sha(gate_row_text(c)),
                )
                for c, v in budgeted
            ]
            counters["bearing_shadow_errors"] += len(rows)
        else:
            rows = await _ask_all(
                budgeted,
                client=client,
                shadow_ref=ref,
                gate_ref=gate_ref,
                counters=counters,
            )
        await write_shadow_rows(
            pool,
            rows,
            run_id=run_id,
            analyst_id=analyst_id,
            target_id=target_id,
            counters=counters,
        )
        logger.info(
            "claim_watch.bearing_shadow.done asked=%d shadowed=%d answered=%d "
            "agree=%d errors=%d skipped=%d deferred=%d rows=%d ref=%s",
            len(asked),
            len(rows),
            counters["bearing_shadow_answered"],
            counters["bearing_shadow_agree"],
            counters["bearing_shadow_errors"],
            counters["bearing_shadow_skipped"],
            counters["bearing_shadow_deferred"],
            counters["bearing_shadow_rows"],
            ref,
        )
        return rows
    except Exception as exc:  # noqa: BLE001 — the last line of the contract
        counters["bearing_shadow_errors"] += 1
        logger.warning("claim_watch.bearing_shadow.failed err=%s", exc)
        return []
