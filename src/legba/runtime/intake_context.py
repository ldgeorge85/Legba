# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which intake request (if any) the current acquisition run is serving.

The drain sets it around a request's run; the follow step reads it so a
reference found in a document an intake run wrote becomes a child of THAT
request, one level deeper. Outside a run (a scheduled poll) it is ``None``
and a followed reference starts at depth 1 under its parent signal.
A leaf: stdlib only, so the document sink can import it without a cycle.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass
from typing import Any, Iterator, Mapping
from uuid import UUID

__all__ = ["IntakeRun", "current_intake_run", "intake_run_scope"]


@dataclass(frozen=True)
class IntakeRun:
    """The request a run serves. ``request_id`` and ``depth`` drive the
    follow step; the rest (I-2) is what a document records about how it
    arrived — the ``page`` kind stamps it as ``payload.intake`` so a reader
    sees "fetched on request, followed from signal X" — and ``publisher``,
    the registered source sharing the address's host (resolved by the run,
    which has the database; the handler does not)."""

    request_id: UUID
    depth: int
    reason: str | None = None
    requested_by: str | None = None
    on_behalf_of: str | None = None
    link_kind: str | None = None
    parent_request_id: UUID | None = None
    parent_signal_id: UUID | None = None
    publisher: Mapping[str, Any] | None = None

    def provenance(self) -> dict[str, Any]:
        """The request as a document records it (JSON-ready)."""
        return {
            "request_id": str(self.request_id), "depth": self.depth, "reason": self.reason,
            "requested_by": self.requested_by, "on_behalf_of": self.on_behalf_of,
            "link_kind": self.link_kind,
            "parent_request_id": str(self.parent_request_id) if self.parent_request_id else None,
            "parent_signal_id": str(self.parent_signal_id) if self.parent_signal_id else None,
        }


_CURRENT: "contextvars.ContextVar[IntakeRun | None]" = contextvars.ContextVar(
    "legba_intake_run", default=None,
)


@contextlib.contextmanager
def intake_run_scope(run: IntakeRun | None) -> Iterator[None]:
    token = _CURRENT.set(run)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_intake_run() -> IntakeRun | None:
    return _CURRENT.get()
