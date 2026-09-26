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
MAX_ACCUMULATED_STEPS = 400

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


def invoke_timeout_seconds() -> float:
    """The detached run's own read timeout against the Dapr sidecar.

    This is the number whose old value (300s) cut the 2026-09-16 run's final
    synthesis in half. It must sit ABOVE the analyst's worst case plus the
    actor's own assembly, so the loop's *own* graceful degradation is what ends
    a long run — never the caller's clock.

    RAISED 600 → 900 by the c8a0105c train, because the analyst's worst case
    grew and 600 stopped clearing it. The arithmetic, which is the only reason
    this constant has a value at all:

        drilling stops at most at  wall_budget (210s)
                                 + one round_deadline (150s)   = 360s
        the synthesis then gets   max(remaining, final_floor)  = 240s floor
                                                        total  = 600s

    — i.e. exactly the old timeout, with zero headroom for the actor envelope.
    A budget that is precisely equal to the clock that kills it is the same
    bug this train exists to remove, one level out. 900 leaves 300s of margin.

    Env: ``LEGBA_CONSULT_INVOKE_TIMEOUT_SECONDS``. Raising the analyst's own
    budgets (``LEGBA_CONSULT_BUDGET_SECONDS``,
    ``LEGBA_CONSULT_FINAL_FLOOR_SECONDS``) means re-checking this sum.
    """
    return _env_float("LEGBA_CONSULT_INVOKE_TIMEOUT_SECONDS", 900.0)


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

    @property
    def elapsed_s(self) -> float:
        return round((self.finished_at or time.monotonic()) - self.started_at, 1)

    def partial_summary(self) -> str:
        """Plain-language account of a run that did not produce an answer.

        This is the CONTENT of the persisted assistant turn on failure, so it
        has to read as something an operator wants to see in a transcript, not
        as a status code. It names what landed, where it stopped, and why.
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
        lines = ["_This consult did not finish._", ""]
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
    ) -> ConsultRun:
        """Launch ``execute`` as a detached run and return its record.

        ``execute`` must return the projected response dict, or raise
        :class:`ConsultRunError` to record a client-facing failure. Any other
        exception is recorded as a 502 — the run is *always* terminal, never
        left ``running`` forever, because a client waiting on the stream has no
        other way to learn the run died.
        """
        self._sweep()
        run = ConsultRun(request_id=request_id, session_id=session_id)
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
            if len(run.steps) >= MAX_ACCUMULATED_STEPS:
                return
            step = {k: v for k, v in frame.items() if k != "type"}
            run.steps.append(step)

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
    try:
        await nats_store.nc.publish(
            f"legba.consult.steps.{request_id}",
            json.dumps(frame, default=str).encode("utf-8"),
        )
    except Exception:  # noqa: BLE001 — telemetry never fails the run
        logger.debug("consult.run.terminal_publish_failed", exc_info=True)


__all__ = [
    "MAX_ACCUMULATED_STEPS",
    "MAX_REMEMBERED_RUNS",
    "RUN_RETENTION_SECONDS",
    "ConsultRun",
    "ConsultRunError",
    "ConsultRunManager",
    "invoke_timeout_seconds",
    "publish_terminal_frame",
    "sync_wait_seconds",
]
