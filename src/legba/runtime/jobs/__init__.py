# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.runtime.jobs — async job plane: NATS work-queue + worker pool (P-07).

The runtime half of the job plane (PIVOT §5.1 competing-consumer execution
shape). Pairs with the data half in :mod:`legba.data.jobs`.

  * :mod:`queue`            — :class:`JobQueue`: JetStream work-queue stream +
                              shared durable pull consumer + enqueue.
  * :mod:`worker`           — :class:`JobWorker` / :class:`JobWorkerPool`:
                              competing consumers; scale by adding workers.
  * :mod:`dispatch`         — :class:`JobDispatch` job-kind → handler registry
                              + :class:`JobContext`.
  * :mod:`process_media`    — the ``process_media`` handler (PIVOT §4.6 tier 3):
                              lands the derived signal AND publishes it back
                              into fan-out (A-2 loop close).
  * :mod:`media_client` — thin hosted transcribe/caption edge (real
                              HTTP only; refuses loudly when no endpoint is
                              configured — no stub fallback).

Submodules import lazily where they pull ``httpx`` / ``nats`` so importing the
package stays cheap (mirrors the :mod:`legba.runtime` import-discipline note).
"""

from .dispatch import JobContext, JobDispatch, default_dispatch
from .queue import JobQueue
from .worker import JobWorker, JobWorkerPool

__all__ = [
    "JobContext",
    "JobDispatch",
    "JobQueue",
    "JobWorker",
    "JobWorkerPool",
    "default_dispatch",
]
