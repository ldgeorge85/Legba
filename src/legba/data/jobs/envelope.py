# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generic async-job envelope (P-07, PIVOT §5.1).

The job plane is the **NATS work-queue worker-pool** execution shape: bounded,
stateless, interchangeable jobs handled by competing consumers (vs. the
addressable, stateful Dapr virtual actors that host targets/analysts/sources).

Every job — media extraction now, crawl/query discovery later — rides ONE
generic envelope so later job kinds reuse the plumbing rather than re-cutting
it. The envelope is the contract between the enqueuer (an analyst tool call, a
source baseline, an operator) and the worker pool. The ``job_kind`` selects the
handler; the worker plumbing never grows kind-specific fields.

Fields (frozen per the P-07 task spec):

  * ``job_id``         — unique id for this job (UUID).
  * ``job_kind``       — handler selector (e.g. ``process_media``).
  * ``requested_by``   — who/what enqueued it (analyst id, source id, operator).
  * ``budget_account`` — the ledger account the job's cost bills against
                         (analyst id, tenant, or a synthetic system account).
  * ``input_refs``     — opaque, kind-specific inputs (the ``process_media``
                         kind reads ``media_ref`` + ``extraction`` +
                         ``derived_from``).
  * ``idempotency_key``— de-dup key. Two jobs with the same key produce the
                         work once; the second observes the first's result.
  * ``attempts``       — delivery attempt counter (incremented on redelivery).
  * ``deadline``       — absolute UTC time after which the job is abandoned.
  * ``tenant_id``      — tenancy seam (mirrors ``Signal.owner_tenant``).

The envelope is JSON-serialised onto a JetStream work-queue subject and JSON-
parsed by the worker. ``model_config`` forbids extra keys so a malformed
envelope fails loud at parse time rather than silently dropping fields.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


# Job kind is an OPEN string — the envelope is generic, and the dispatch table
# (:class:`legba.runtime.jobs.JobDispatch`) is the single source of truth for
# which kinds have handlers. ``KNOWN_JOB_KINDS`` documents the kinds in the
# tree: ``process_media`` is the one shipped kind. (``crawl_discovery`` /
# ``query_discovery`` were DROPPED per decision F-1, 2026-06-09 — they were
# enqueueable but no handler ever consumed them, a terminal "no handler"
# failure dressed as an enqueue; job-based deep-crawl discovery is a
# designed direction item, docs/DIRECTION.md §8.)
JobKind = str

KNOWN_JOB_KINDS: tuple[str, ...] = (
    "process_media",
)

# NOTE (A-2): the former ``result_sink`` field is GONE. It declared three
# sinks (``derived_signal`` / ``nats`` / ``none``) but no handler ever read
# it — a half-state contract. Where a result lands is the HANDLER's documented
# behavior (``process_media`` lands a derived signal AND publishes it back
# into fan-out); a future kind that needs a different sink adds it then.


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class JobEnvelope(BaseModel):
    """The generic work-queue job envelope. One shape for every job kind."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID = Field(default_factory=uuid4)
    job_kind: str = Field(..., min_length=1)
    requested_by: str = "system"
    budget_account: str = "system"
    input_refs: dict[str, Any] = Field(default_factory=dict)
    # Defaults to job_id so a caller that omits it still gets at-least-once →
    # effectively-once on the job_id; a caller that sets it (the common case)
    # collapses semantically-identical work.
    idempotency_key: str = ""
    attempts: int = Field(default=0, ge=0)
    deadline: datetime | None = None
    tenant_id: str = "default"
    enqueued_at: datetime = Field(default_factory=_utcnow)

    def model_post_init(self, _ctx: Any) -> None:  # noqa: D401
        # An empty idempotency_key defaults to the job_id (effectively-once on
        # the job identity). Set on the instance via object.__setattr__ since
        # the model isn't frozen but pydantic v2 routes plain assignment
        # through validation only when validate_assignment is on.
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", str(self.job_id))

    # ------------------------------------------------------------------
    # Wire (de)serialisation — JSON bytes on the NATS work-queue.
    # ------------------------------------------------------------------

    def to_bytes(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "JobEnvelope":
        return cls.model_validate_json(raw)

    def is_expired(self, *, now: datetime | None = None) -> bool:
        if self.deadline is None:
            return False
        return (now or _utcnow()) > self.deadline


class JobResult(BaseModel):
    """A worker's outcome for one job.

    ``status`` is the worker-pool-level outcome (the handler succeeded /
    failed / the job was a no-op idempotent replay). ``output_refs`` carries
    handler-specific results (e.g. the derived signal id that landed).
    """

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    job_kind: str
    status: Literal["completed", "failed", "skipped_duplicate", "expired"]
    output_refs: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    worker_id: str = ""
    finished_at: datetime = Field(default_factory=_utcnow)

    def to_bytes(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "JobResult":
        return cls.model_validate_json(raw)

    @classmethod
    def from_json_row(cls, raw: "str | dict[str, Any] | None") -> "JobResult | None":
        """Parse the ``legba_jobs.result`` jsonb column.

        Arrives as ``dict`` from the codec-bearing pool, as ``str`` from a
        codec-less connection — accept both.
        """
        if not raw:
            return None
        if isinstance(raw, str):
            raw = json.loads(raw)
        return cls.model_validate(raw)


__all__ = ["JobEnvelope", "JobKind", "JobResult", "KNOWN_JOB_KINDS"]
