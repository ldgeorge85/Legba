# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Detached consult runs — the work outlives the caller (D-7).

Why this module exists
======================

Before this, ``POST /api/v1/consult`` *was* the run: the request handler held
an ``httpx`` PUT open against the Dapr sidecar for the entire ReAct loop and
returned the answer on that same connection. Two things followed from that,
and both of them lost a real answer on 2026-09-16:

1. **The caller's timeout killed the actor.** ``DAPR_INVOKE_TIMEOUT_SECONDS``
   (300s) is a client-side read timeout. When it fired, ``httpx`` closed the
   TCP connection to the sidecar; daprd cancelled the in-flight actor method;
   the runtime tore the actor down and re-activated it seconds later. The
   ReAct loop was *mid-final-synthesis*. Nothing was written.
2. **For a chat consult, the registry is the only writer.** A ``mode=chat``
   run short-circuits in the actor (``dapr_actors`` chat branch): no
   ``analyst_outputs`` row, no ``analyst_traces`` row, no receipt chain — the
   typed payload comes back IN the invoke envelope and the *registry* is what
   persists it as the assistant turn. So an answer that never reaches the
   registry has never existed anywhere durable.

The fix is to separate the *run* from the *request*. The POST starts a run
owned by this manager and returns; the run holds its own client, its own
timeout tied to the analyst's wall-clock budget, and finishes whether or not
anyone is still listening. Persistence is shielded from cancellation, so even
a deliberate cancel writes whatever the run already had.

Mechanism choice (and why not the alternatives)
-----------------------------------------------

*A registry-owned ``asyncio`` task* is what runs the invoke. The thing that
must survive is exactly one resource — the registry→sidecar HTTP connection —
and an asyncio task in the registry process owns it outright: a browser
closing its EventSource, or navigating away, touches nothing the task holds.

The alternatives were weighed and rejected:

* **A one-shot Dapr actor reminder.** It would make the actor self-driving,
  but reminders are fire-and-forget: they return no value, so the typed
  ConsultResponsePayload the chat branch puts in its envelope would have
  nowhere to go, and the actor would have to grow its own persistence path.
  That lives in ``runtime/dapr_actors.py``, which is frozen for R4.
* **A Dapr Workflow**, as ``deep_consult`` uses. Correct for a multi-stage
  detached task, far too heavy for a chat turn that wants a sub-second
  acknowledgement, and again a runtime-side change.
* **Leaving it synchronous with a bigger timeout.** Moves the cliff, doesn't
  remove it, and still pins a request worker for minutes.

The honest limitation: a registry *restart* mid-run drops the in-flight task,
and for a chat run the answer is lost (deep runs survive — their workflow owns
the write). A restart also drops every SSE connection, so the blast radius is
the same either way; recovering across a restart would need a durable run
table, which is a migration and is deliberately not in this change.

Delivery — and why the run watches its own step stream
------------------------------------------------------

The panel must never go blank. That rules out a run that only reports its
ending: when a run dies at round 5, what the operator needs to see is the four
rounds that *did* land and the reason the fifth didn't.

The steps exist — the actor publishes each one to
``legba.consult.steps.<request_id>`` — but only as live core-NATS traffic with
no replay, so they exist only for whoever happens to be attached. So the run
attaches too, for its whole life, and accumulates them. That gives the
registry a partial trace it can *persist* on failure, which is what turns
"the tab was closed when it broke" from a blank turn into a readable one.

When a run finishes the manager does three things, in this order:

1. Persists the assistant turn (``consult_persistence.append_turn``) under
   ``asyncio.shield`` — the durable artifact. On failure this is the partial:
   the accumulated steps plus a plain-language reason.
2. Publishes a terminal frame on the run's step subject carrying the whole
   projected response (or the failure + partial), so a listening browser
   renders without a round-trip. The actor already publishes a *bare* ``final``
   frame with no answer in it; this one is tagged ``final_source: "registry"``
   and is what the relay treats as terminal.
3. Keeps the result in memory for ``RUN_RETENTION_SECONDS`` so
   ``GET /consult/runs/{request_id}`` can answer a client that reconnected
   after the frame went out.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from .. import consult_clocks as _clocks

logger = logging.getLogger(__name__)

#: How long a finished run stays queryable via ``GET /consult/runs/{id}``.
#: The durable record is the persisted turn; this is only the fast path for a
#: client that dropped its stream between the answer and the reconnect.
RUN_RETENTION_SECONDS = 900.0

#: Hard cap on remembered runs, so a busy registry cannot grow this unbounded
#: even if the sweep never fires. Oldest-finished are evicted first.
MAX_REMEMBERED_RUNS = 256

#: Cap on steps accumulated per run. The analyst's own round ceiling bounds the
#: real number; this only stops a runaway publisher from growing the record.
#: ``answer_delta`` frames do NOT count against it (see
#: :data:`MAX_ACCUMULATED_ANSWER_CHARS`): they are the answer itself, and a
#: long tool-heavy run must not lose its answer to the trace that preceded it.
MAX_ACCUMULATED_STEPS = 400

#: Cap on the streamed answer text (``answer_delta`` frames) a run holds. The
#: provider's own output-token cap bounds the real number; this only bounds a
#: runaway publisher. Two million characters is far past any real answer.
MAX_ACCUMULATED_ANSWER_CHARS = 2_000_000

RunStatus = Literal["running", "complete", "error"]


def _env_float(name: str, default: float) -> float:
    """A positive float from the environment, else ``default``.

    Unset / empty / non-numeric / non-positive all fall back — a malformed pin
    must never zero a timeout.
    """
    raw = os.getenv(name, "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return default


def sync_wait_seconds() -> float:
    """How long ``POST /consult`` waits for a fast answer before it returns 202.

    A short consult (one survey round, no drilling) commonly lands inside this
    window, and returning its answer on the POST keeps the common case a single
    round-trip AND keeps the pre-202 response contract byte-identical for it.
    Past this the client gets 202 + the request_id and reads the answer off the
    stream it already has open. Env: ``LEGBA_CONSULT_SYNC_WAIT_SECONDS``.
    """
    return _env_float("LEGBA_CONSULT_SYNC_WAIT_SECONDS", 20.0)


#: Margin the invoke timeout keeps ABOVE the loop's worst case, for the actor's
#: own envelope assembly and the persist hop back. Never zero: a budget that is
#: precisely equal to the clock that kills it is the bug this exists to remove.
INVOKE_TIMEOUT_MARGIN_SECONDS = 300.0

#: The env pins already warned about, so a below-floor pin is said LOUDLY once
#: per (pin, floor) pair rather than on every consult.
_warned_floor_pins: set[tuple[float, float]] = set()


def invoke_timeout_floor_seconds() -> float:
    """The least invoke timeout the loop's own clocks allow.

    Read through the SAME env readers the loop runs under
    (:mod:`legba.data.consult_clocks`, re-exported by ``consult_round_protocol``):

        drilling never runs past        total_budget   (480s default)
        the synthesis then gets   max(remaining, final_floor)  (240s floor)
        the actor envelope + persist hop gets the margin    (300s)
                                                        floor = 1020s

    Deriving it, rather than documenting a sum an operator has to re-check, is
    the run 9b902fec fix: ``LEGBA_CONSULT_BUDGET_SECONDS=900`` beside a 900s
    invoke timeout cut a 26 KB answer one second before the loop's own final
    slice would have delivered it.
    """
    return (
        _clocks.default_total_budget_seconds()
        + _clocks.default_final_floor_seconds()
        + INVOKE_TIMEOUT_MARGIN_SECONDS
    )


def invoke_timeout_seconds() -> float:
    """The detached run's own read timeout against the Dapr sidecar.

    This is the number whose old value (300s) cut the 2026-09-16 run's final
    synthesis in half. It must sit ABOVE the analyst's worst case plus the
    actor's own assembly, so the loop's *own* graceful degradation is what ends
    a long run — never the caller's clock.

    It is now ``max(env_pin, floor)`` where the floor is
    :func:`invoke_timeout_floor_seconds` — ``total_budget + final_floor + 300``
    off the loop's own env readers. With nothing set that is
    ``480 + 240 + 300 = 1020s``, which beats the 900s default pin: the derived
    floor wins, by design. An env pin below the floor is RAISED to it and
    logged once, loudly, naming both numbers — the two clocks can never be
    equal again, whatever the operator sets.

    Env: ``LEGBA_CONSULT_INVOKE_TIMEOUT_SECONDS`` (default 900). Raising the
    analyst's own budgets (``LEGBA_CONSULT_BUDGET_SECONDS``,
    ``LEGBA_CONSULT_FINAL_FLOOR_SECONDS``) raises this with them.
    """
    pinned = _env_float("LEGBA_CONSULT_INVOKE_TIMEOUT_SECONDS", 900.0)
    floor = invoke_timeout_floor_seconds()
    if pinned >= floor:
        return pinned
    key = (pinned, floor)
    if key not in _warned_floor_pins and os.getenv("LEGBA_CONSULT_INVOKE_TIMEOUT_SECONDS", "").strip():
        _warned_floor_pins.add(key)
        logger.warning(
            "consult.invoke_timeout.below_loop_floor "
            "LEGBA_CONSULT_INVOKE_TIMEOUT_SECONDS=%.0fs is below the consult "
            "loop's own worst case (total budget %.0fs + final floor %.0fs + "
            "%.0fs margin = %.0fs); using %.0fs so the loop's partial delivery, "
            "not the registry's clock, ends a long run",
            pinned,
            _clocks.default_total_budget_seconds(),
            _clocks.default_final_floor_seconds(),
            INVOKE_TIMEOUT_MARGIN_SECONDS,
            floor,
            floor,
        )
    return floor


@dataclass
class ConsultRun:
    """One detached consult run's state."""

    request_id: str
    session_id: str | None
    status: RunStatus = "running"
    #: The projected ConsultResponse (as a dict) once complete.
    result: dict[str, Any] | None = None
    #: ``(status_code, detail)`` once failed — the same pair the synchronous
    #: path would have raised as an HTTPException.
    error: tuple[int, Any] | None = None
    #: Step frames seen on the run's subject, in arrival order. The partial
    #: trace a failed run persists so the panel has something to render.
    steps: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    task: asyncio.Task[Any] | None = None
    #: C-1b — the case-frame accumulator for a turn the operator ARMED
    #: (``present_case``); ``None`` for every plain turn, which is then exactly
    #: what it was. Fed from the same step watch as ``steps``, NOT capped by
    #: :data:`MAX_ACCUMULATED_STEPS` (a case is capped by its own gate).
    case: Any | None = None
    #: Characters of streamed answer (``answer_delta`` frames) held in
    #: ``steps``, and how many such frames — the step cap excludes them.
    answer_chars: int = 0
    answer_frames: int = 0

    @property
    def elapsed_s(self) -> float:
        return round((self.finished_at or time.monotonic()) - self.started_at, 1)

    def accumulate(self, step: dict[str, Any]) -> None:
        """Append one step frame under the run's caps.

        ``answer_delta`` frames are the answer, not the trace: they are bounded
        by :data:`MAX_ACCUMULATED_ANSWER_CHARS` and never count against
        :data:`MAX_ACCUMULATED_STEPS`, so a tool-heavy run cannot lose the
        answer it was writing to the frames that preceded it.
        """
        if step.get("kind") == "answer_delta":
            text = step.get("text")
            if not isinstance(text, str) or not text:
                return
            if self.answer_chars + len(text) > MAX_ACCUMULATED_ANSWER_CHARS:
                return
            self.answer_chars += len(text)
            self.answer_frames += 1
            self.steps.append(step)
            return
        if len(self.steps) - self.answer_frames >= MAX_ACCUMULATED_STEPS:
            return
        self.steps.append(step)

    def _answer_deltas(self) -> list[dict[str, Any]]:
        return [
            s for s in self.steps
            if s.get("kind") == "answer_delta" and isinstance(s.get("text"), str)
        ]

    def streamed_answer(self) -> str:
        """The answer text the run streamed before it stopped, or ``""``.

        The concatenated ``text`` of the run's ``answer_delta`` frames, in
        arrival order. The loop tags each frame with the ``segment`` that
        produced it (``round-<n>`` for a drilling round, ``final`` for the
        forced synthesis); only the LAST segment is the answer in flight — an
        earlier one is a tool round's preamble, written before the model called
        its tools. Frames with no segment (an older runtime) are one segment.
        """
        deltas = self._answer_deltas()
        if not deltas:
            return ""
        last = deltas[-1].get("segment")
        return "".join(s["text"] for s in deltas if s.get("segment") == last)

    def partial_summary(self) -> str:
        """Plain-language account of a run that did not produce an answer.

        This is the CONTENT of the persisted assistant turn on failure, so it
        has to read as something an operator wants to see in a transcript, not
        as a status code. It names what landed, where it stopped, and why.

        When the run STREAMED answer text before it was cut, that text comes
        FIRST, under a one-line note saying it was cut and where. Run 9b902fec
        (2026-10-29) held 26 KB of answer in this run's own step frames while
        the persisted turn read "What it had gathered: <tool list>". Paid paths
        deliver the partial; the account of the stop follows it.
        """
        rounds = sorted(
            {
                int(s["round"])
                for s in self.steps
                if isinstance(s.get("round"), int)
            }
        )
        tools = [s for s in self.steps if s.get("kind") == "tool_call"]
        detail = ""
        if self.error is not None:
            detail = str(self.error[1])
        lines: list[str] = []
        streamed = self.streamed_answer().strip()
        if streamed:
            segment = self._answer_deltas()[-1].get("segment")
            if segment == "final":
                where = "during its final synthesis"
            elif isinstance(segment, str) and segment.startswith("round-"):
                where = f"while answering in round {segment[len('round-'):]}"
            else:
                where = "while it was writing its answer"
            lines.append(
                f"_This consult was cut {where}, after {self.elapsed_s:.0f}s; "
                f"below is the {len(streamed):,} characters of answer it had "
                f"written, unfinished._"
            )
            lines.extend(["", streamed, "", "---", ""])
        lines.extend(["_This consult did not finish._", ""])
        if rounds:
            lines.append(
                f"It ran {len(rounds)} round(s) and completed "
                f"{len(tools)} tool call(s) before it stopped at round "
                f"{rounds[-1]}."
            )
        else:
            lines.append("It stopped before completing any round.")
        if detail:
            lines.append("")
            lines.append(f"Reason: {detail}")
        if tools:
            lines.append("")
            lines.append("What it had gathered:")
            for step in tools[:20]:
                name = step.get("tool") or "(tool)"
                ok = "ok" if step.get("ok") else "failed"
                lines.append(f"- `{name}` — {ok}")
        lines.append("")
        lines.append(
            "The full step trace is on this turn; re-ask to run it again."
        )
        return "\n".join(lines)

    def as_status_payload(self) -> dict[str, Any]:
        """The shape ``GET /consult/runs/{request_id}`` returns."""
        payload: dict[str, Any] = {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "status": self.status,
            "elapsed_s": self.elapsed_s,
            "steps": list(self.steps),
        }
        if self.result is not None:
            payload["response"] = self.result
        if self.error is not None:
            payload["error_status"] = self.error[0]
            payload["error_detail"] = self.error[1]
            # The same partial the terminal frame carries, so a client that
            # reconnects after that frame went out renders the streamed answer
            # rather than "the server recorded no partial".
            payload["partial_answer"] = self.partial_summary()
        return payload


class ConsultRunManager:
    """Owns the detached run tasks and their short-lived results.

    One instance per registry app, held on the consult router closure. Not
    thread-safe by design — it lives on the single asyncio loop the registry
    serves from.
    """

    def __init__(
        self,
        *,
        nats_store: Any | None = None,
        retention_seconds: float = RUN_RETENTION_SECONDS,
        max_remembered: int = MAX_REMEMBERED_RUNS,
    ) -> None:
        self._runs: dict[str, ConsultRun] = {}
        self._nats_store = nats_store
        self._retention = retention_seconds
        self._max_remembered = max_remembered

    # ---- lookup ---------------------------------------------------------

    def get(self, request_id: str) -> ConsultRun | None:
        self._sweep()
        return self._runs.get(request_id)

    # ---- lifecycle ------------------------------------------------------

    def start(
        self,
        *,
        request_id: str,
        session_id: str | None,
        execute: Callable[[], Awaitable[dict[str, Any]]],
        on_complete: Callable[[ConsultRun], Awaitable[None]] | None = None,
        case: Any | None = None,
    ) -> ConsultRun:
        """Launch ``execute`` as a detached run and return its record.

        ``execute`` must return the projected response dict, or raise
        :class:`ConsultRunError` to record a client-facing failure. Any other
        exception is recorded as a 502 — the run is *always* terminal, never
        left ``running`` forever, because a client waiting on the stream has no
        other way to learn the run died.
        """
        self._sweep()
        run = ConsultRun(request_id=request_id, session_id=session_id, case=case)
        self._runs[request_id] = run
        run.task = asyncio.ensure_future(self._drive(run, execute, on_complete))
        return run

    async def _watch_steps(self, run: ConsultRun) -> Any | None:
        """Subscribe to the run's step subject and accumulate frames.

        Returns the subscription (to be unsubscribed by the caller) or None
        when there is no NATS store — in which case the run simply has no
        partial trace to persist, and degrades to today's behaviour.
        """
        if self._nats_store is None:
            return None

        async def _on_msg(msg: Any) -> None:
            try:
                frame = json.loads(msg.data)
            except Exception:  # noqa: BLE001 — a malformed frame is not fatal
                return
            if not isinstance(frame, dict) or frame.get("type") != "step":
                return
            step = {k: v for k, v in frame.items() if k != "type"}
            if run.case is not None:
                # Before the cap check: the record must not depend on how
                # chatty the trace was. Marks a post-stop frame ``after_stop``.
                run.case.observe(step)
            run.accumulate(step)

        try:
            return await self._nats_store.nc.subscribe(
                f"legba.consult.steps.{run.request_id}", cb=_on_msg,
            )
        except Exception:  # noqa: BLE001 — telemetry never fails the run
            logger.debug("consult.run.step_watch_failed", exc_info=True)
            return None

    async def _drive(
        self,
        run: ConsultRun,
        execute: Callable[[], Awaitable[dict[str, Any]]],
        on_complete: Callable[[ConsultRun], Awaitable[None]] | None,
    ) -> None:
        sub = await self._watch_steps(run)
        try:
            run.result = await execute()
            run.status = "complete"
        except ConsultRunError as exc:
            run.status = "error"
            run.error = (exc.status_code, exc.detail)
            logger.warning(
                "consult.run.failed request_id=%s status=%d detail=%s",
                run.request_id, exc.status_code, str(exc.detail)[:512],
            )
        except asyncio.CancelledError:
            run.status = "error"
            run.error = (
                499,
                "the consult run was cancelled before it produced an answer",
            )
            logger.warning("consult.run.cancelled request_id=%s", run.request_id)
            # Deliver the partial BEFORE the cancellation resumes unwinding —
            # a cancelled run is exactly the case whose trace would otherwise
            # vanish, which is the 2026-09-16 loss.
            run.finished_at = time.monotonic()
            await self._deliver(run, on_complete, sub)
            raise
        except Exception as exc:  # noqa: BLE001 — a run is always terminal
            run.status = "error"
            run.error = (502, f"consult run failed: {exc}")
            logger.exception("consult.run.unhandled request_id=%s", run.request_id)
        else:
            pass
        run.finished_at = time.monotonic()
        await self._deliver(run, on_complete, sub)

    async def _deliver(
        self,
        run: ConsultRun,
        on_complete: Callable[[ConsultRun], Awaitable[None]] | None,
        sub: Any | None,
    ) -> None:
        """Run the delivery hook once, shielded, then drop the subscription."""
        if on_complete is not None:
            try:
                # Delivery (persist + terminal frame) must not be skipped by a
                # cancel landing between the answer and the write.
                await asyncio.shield(on_complete(run))
            except Exception:  # noqa: BLE001 — delivery is best-effort
                logger.exception(
                    "consult.run.deliver_failed request_id=%s", run.request_id,
                )
        if sub is not None:
            try:
                await sub.unsubscribe()
            except Exception:  # noqa: BLE001 — best-effort teardown
                logger.debug("consult.run.unsubscribe_failed", exc_info=True)

    # ---- retention ------------------------------------------------------

    def _sweep(self) -> None:
        """Drop finished runs past retention, then cap the remembered set."""
        now = time.monotonic()
        stale = [
            rid
            for rid, run in self._runs.items()
            if run.finished_at is not None
            and now - run.finished_at > self._retention
        ]
        for rid in stale:
            self._runs.pop(rid, None)
        if len(self._runs) <= self._max_remembered:
            return
        finished = sorted(
            (r for r in self._runs.values() if r.finished_at is not None),
            key=lambda r: r.finished_at or 0.0,
        )
        overflow = len(self._runs) - self._max_remembered
        for run in finished[:overflow]:
            self._runs.pop(run.request_id, None)


class ConsultRunError(Exception):
    """A client-facing failure inside a detached run.

    Carries the ``(status_code, detail)`` the synchronous path would have
    raised as an ``HTTPException`` — so the 202 flow reports exactly the same
    errors the 200 flow did, just over the stream / status endpoint instead of
    the POST response.
    """

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


async def publish_terminal_frame(
    nats_store: Any | None,
    *,
    request_id: str,
    run: ConsultRun,
) -> None:
    """Publish the registry's terminal frame for ``run`` on its step subject.

    This is the frame that carries the ANSWER. The actor's own ``final`` frame
    (published from ``dapr_actors`` before the method returns) carries only
    ``output_id`` / ``mode`` — a listening browser learned the run had ended but
    still had to get the text from the POST response, which is precisely what a
    504 destroyed. ``final_source: "registry"`` is what the SSE relay keys its
    close on.

    Fire-and-forget on core NATS: no subscriber means the frame is dropped, and
    the persisted turn remains the record of what happened.
    """
    if nats_store is None:
        return
    frame: dict[str, Any] = {
        "type": "final",
        "final_source": "registry",
        "request_id": request_id,
        "status": run.status,
    }
    if run.session_id:
        frame["session_id"] = run.session_id
    if run.result is not None:
        frame["response"] = run.result
    if run.error is not None:
        frame["error_status"] = run.error[0]
        frame["error_detail"] = run.error[1]
        # The partial the panel renders instead of a bare error string.
        frame["partial_answer"] = run.partial_summary()
        frame["steps"] = list(run.steps)
        if run.case is not None:
            # C-1b — the case as the registry recorded it (stopped / unsealed,
            # with any gap named), so the player reconciles without a reload.
            frame["case"] = run.case.compose()
    try:
        await nats_store.nc.publish(
            f"legba.consult.steps.{request_id}",
            json.dumps(frame, default=str).encode("utf-8"),
        )
    except Exception:  # noqa: BLE001 — telemetry never fails the run
        logger.debug("consult.run.terminal_publish_failed", exc_info=True)


__all__ = [
    "MAX_ACCUMULATED_ANSWER_CHARS",
    "MAX_ACCUMULATED_STEPS",
    "MAX_REMEMBERED_RUNS",
    "RUN_RETENTION_SECONDS",
    "ConsultRun",
    "ConsultRunError",
    "ConsultRunManager",
    "INVOKE_TIMEOUT_MARGIN_SECONDS",
    "invoke_timeout_floor_seconds",
    "invoke_timeout_seconds",
    "publish_terminal_frame",
    "sync_wait_seconds",
]
