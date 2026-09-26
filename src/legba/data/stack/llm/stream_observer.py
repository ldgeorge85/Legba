# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live text-delta capture for a generation that may not be allowed to finish.

Why this exists
===============

Run c8a0105c (2026-09-16) burned ~$10 and delivered 487 characters of apology.
The loop drilled for 314s, the final synthesis got a fixed 150s slice, Fable
did not finish a long answer inside it, and ``asyncio.wait_for`` **cancelled**
the call. Cancellation is the problem: the Anthropic handler had already
streamed most of that answer off the wire and accumulated it, and every byte of
it was already generated and BILLED — but the accumulator's return value is
only produced when the stream ends, so a cancel at 150s threw the whole partial
generation away and left the operator with a replacement message.

The handler streams. What it did not have was a way to let a caller *watch*
the text arrive, so that a caller who is cut off still holds what was paid for.

That is all this module is: a context-local sink that
:meth:`AnthropicProviderHandler._accumulate_stream` publishes ``text_delta``
chunks into. It is deliberately NOT a change to the handler's return contract,
its retry contract, or its streaming semantics — a sink that nobody installed
costs one ``ContextVar.get()`` per delta and changes nothing.

Why a ContextVar rather than a parameter
----------------------------------------

The alternative was threading an ``on_text_delta`` keyword from the consult
loop through ``chat_complete`` → ``_call_chat`` → ``_call_chat_streaming`` →
``_accumulate_stream``. That crosses four signatures in a handler shared by
every analyst kind, and ``chat_complete`` forwards ``**kwargs`` into the wire
payload builder — an unrecognised keyword there is a provider 400, so the
threading would have to be special-cased at each hop. A context-local sink
crosses none of them.

The cancellation behaviour is the point of the design, and it is load-bearing:

* ``asyncio.wait_for`` wraps its awaitable in a Task, and a Task **copies the
  current context at creation**. The sink installed before the ``wait_for``
  call is therefore visible inside the call.
* The sink OBJECT is shared by reference, so deltas appended inside the task
  mutate the list the caller is holding.
* When the timeout fires and the task is cancelled, the caller's sink still
  holds every delta that arrived. The generation was billed; now it is also
  delivered.

Concurrency is safe by construction: each run's task carries its own copied
context, so two consults streaming at once cannot see each other's sink.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

#: Rough characters-per-token for the running estimate. Only ever used for the
#: *live* progress figure shown while a generation is in flight; the persisted
#: receipt always uses the provider's own reported usage, never this.
CHARS_PER_TOKEN = 4.0


@dataclass
class TextDeltaSink:
    """Accumulates streamed assistant text, with an optional live callback.

    ``on_delta`` is invoked per chunk so a transport (the consult SSE stream)
    can relay tokens as they arrive. It is called inside the provider's read
    loop, so it must be cheap and non-blocking — the consult stream enqueues
    onto an unbounded queue, which qualifies. It must also never raise: an
    exception here would surface as a mid-stream transport failure and be
    misread as a provider fault, so :meth:`feed` swallows it.
    """

    parts: list[str] = field(default_factory=list)
    on_delta: Callable[[str], None] | None = None

    @property
    def text(self) -> str:
        """Everything streamed so far, in arrival order."""
        return "".join(self.parts)

    @property
    def chars(self) -> int:
        return sum(len(p) for p in self.parts)

    @property
    def approx_tokens(self) -> int:
        """Character-based token estimate for the live progress figure."""
        return int(self.chars / CHARS_PER_TOKEN)

    def feed(self, chunk: str) -> None:
        """Record one delta. Never raises — see the class docstring."""
        if not chunk:
            return
        self.parts.append(chunk)
        if self.on_delta is not None:
            try:
                self.on_delta(chunk)
            except Exception:  # noqa: BLE001 - a relay fault must not kill a generation
                pass


_SINK: ContextVar[TextDeltaSink | None] = ContextVar(
    "legba_llm_text_delta_sink", default=None,
)


@contextmanager
def capture_text_deltas(sink: TextDeltaSink) -> Iterator[TextDeltaSink]:
    """Install ``sink`` for the duration of the block.

    Restores the previous sink on exit, so nesting (a synthesis inside a run
    that is itself observed) behaves.
    """
    token = _SINK.set(sink)
    try:
        yield sink
    finally:
        _SINK.reset(token)


def publish_text_delta(chunk: str) -> None:
    """Publish one streamed text chunk to the installed sink, if any.

    Called from the provider read loop. A no-op (one ``ContextVar.get()``) when
    nothing is observing, which is the overwhelmingly common case.
    """
    sink = _SINK.get()
    if sink is not None:
        sink.feed(chunk)


def active_sink() -> TextDeltaSink | None:
    """The sink installed in this context, or ``None``."""
    return _SINK.get()


__all__ = [
    "CHARS_PER_TOKEN",
    "TextDeltaSink",
    "active_sink",
    "capture_text_deltas",
    "publish_text_delta",
]
