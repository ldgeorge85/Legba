# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Job-kind → handler dispatch (P-07).

The worker pool is generic: it pulls a :class:`JobEnvelope`, looks up the
handler for ``env.job_kind`` here, and runs it. Adding a new job kind = writing
a handler + registering it — the queue / worker / ledger plumbing is untouched.

A handler is an async callable ``(env, ctx) -> JobResult``. The
:class:`JobContext` carries the shared substrate + the hosted media client so a
handler never reaches for globals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ...data.jobs.envelope import JobEnvelope, JobResult

if TYPE_CHECKING:  # pragma: no cover — typing only
    from ...data.postgres import PostgresStore
    from .media_client import MediaClient
    from .queue import JobQueue


@dataclass
class JobContext:
    """Shared dependencies handed to every job handler.

    ``pg`` is the source-first substrate (signals + the job ledger). ``media``
    is the hosted media-extraction edge. ``queue`` lets a handler re-enqueue (a
    crawl saga fans out child jobs); ``worker_id`` labels the result.
    ``subscriptions`` is the W2 :class:`SubscriptionEngine` — a handler that
    lands a derived signal MUST publish it back into fan-out through this
    (A-2 loop close); ``process_media`` refuses to run without it.
    """

    pg: "PostgresStore"
    media: "MediaClient | None" = None
    queue: "JobQueue | None" = None
    worker_id: str = ""
    subscriptions: Any | None = None


JobHandler = Callable[[JobEnvelope, JobContext], Awaitable[JobResult]]


class JobDispatch:
    """Registry of job-kind handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}

    def register(self, job_kind: str, handler: JobHandler) -> None:
        if job_kind in self._handlers:
            raise ValueError(f"job kind {job_kind!r} already registered")
        self._handlers[job_kind] = handler

    def handler_for(self, job_kind: str) -> JobHandler | None:
        return self._handlers.get(job_kind)

    @property
    def kinds(self) -> list[str]:
        return sorted(self._handlers)


def default_dispatch() -> JobDispatch:
    """A dispatch with the P-07 ``process_media`` kind registered."""
    from .process_media import process_media_handler

    d = JobDispatch()
    d.register("process_media", process_media_handler)
    return d


__all__ = ["JobContext", "JobDispatch", "JobHandler", "default_dispatch"]
