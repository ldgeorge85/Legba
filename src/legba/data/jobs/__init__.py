# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.jobs — async-job-plane data contracts + substrate writes (P-07).

The data half of the job plane (PIVOT §5.1 worker-pool execution shape):

  * :mod:`envelope`  — the generic :class:`JobEnvelope` + :class:`JobResult`.
  * :mod:`store`     — the :class:`JobStore` (idempotency ledger + derived-
                       signal landing against the source-first substrate).
  * :mod:`media`     — ``process_media`` job types + the derived-signal builder.

The runtime half (NATS work-queue, worker pool, hosted client, dispatch) lives in
:mod:`legba.runtime.jobs`.
"""

from .envelope import (
    KNOWN_JOB_KINDS,
    JobEnvelope,
    JobKind,
    JobResult,
)
from .media import (
    DEFAULT_EXTRACTION,
    MediaEndpointNotConfiguredError,
    MediaExtraction,
    MediaExtractionResult,
    ProcessMediaInput,
    build_derived_signal,
    configured_media_endpoint,
)
from .store import ClaimResult, JobStore

__all__ = [
    "ClaimResult",
    "DEFAULT_EXTRACTION",
    "KNOWN_JOB_KINDS",
    "JobEnvelope",
    "JobKind",
    "JobResult",
    "JobStore",
    "MediaEndpointNotConfiguredError",
    "MediaExtraction",
    "MediaExtractionResult",
    "ProcessMediaInput",
    "build_derived_signal",
    "configured_media_endpoint",
]
