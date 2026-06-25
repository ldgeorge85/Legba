# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured JSON logging for the runtime (resilience-observability W-1b §3).

The runtime previously configured logging with a bare
``logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")``.
That is fine for a human at a terminal but opaque to a log pipeline: there is no
machine-parseable structure and, critically, no way to *correlate* a burst of
lines back to the analyst run that produced them.

This module adds two things, both stdlib-only (no new dependency):

  * :class:`JsonLogFormatter` — emits one JSON object per record with the
    standard fields (ts / level / logger / message) plus any bound correlation
    context and any extra ``logging`` kwargs.
  * A :mod:`contextvars`-backed correlation context (:func:`bind_run_context` /
    :func:`current_run_context`) wired into every record by :class:`ContextFilter`.
    The analyst run path binds ``run_id`` (and an optional ``correlation_id``)
    once at the top of a run; every log line emitted underneath — across awaits,
    in any module — then carries those ids automatically.

``configure_structured_logging`` installs the formatter + filter on the root
handler. It is the single entry point the runtime ``main()`` calls instead of
``basicConfig``. JSON is the default; set ``LEGBA_LOG_FORMAT=text`` to keep the
old human format (the correlation ids are still appended to the message), which
is convenient for local ``docker logs`` tailing.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as _dt
import json
import logging
import os
from contextvars import ContextVar
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Correlation context — bound per-run, read by the log filter
# ---------------------------------------------------------------------------

# A run binds {"run_id": ..., "correlation_id": ..., "analyst_id": ...} once;
# everything logged underneath inherits it. ContextVars are task-local, so
# concurrent analyst runs on the same event loop never cross-contaminate.
_run_context: ContextVar[dict[str, str]] = ContextVar("legba_run_context", default={})

# Standard LogRecord attributes — anything NOT in here that lands in
# record.__dict__ was passed via logging's ``extra=`` and is treated as a
# structured field worth emitting.
_RESERVED = frozenset(
    logging.makeLogRecord({}).__dict__.keys()
    | {"message", "asctime", "taskName"}
)


def current_run_context() -> dict[str, str]:
    """Return a copy of the correlation context bound to the current task."""
    return dict(_run_context.get())


@contextlib.contextmanager
def bind_run_context(**fields: Any) -> Iterator[None]:
    """Bind correlation fields for the duration of the ``with`` block.

    Merges ``fields`` onto whatever is already bound (so an inner scope can add
    a ``correlation_id`` without dropping the outer ``run_id``), stringifying
    each value, and restores the previous context on exit. ``None`` values are
    dropped so callers can pass optional ids unconditionally.
    """
    merged = dict(_run_context.get())
    for key, value in fields.items():
        if value is None:
            continue
        merged[key] = str(value)
    token = _run_context.set(merged)
    try:
        yield
    finally:
        _run_context.reset(token)


def bind_run_log_context(**fields: Any) -> "contextvars.Token[dict[str, str]]":
    """Token-based variant of :func:`bind_run_context` for non-``with`` scopes.

    Some call sites (e.g. a long ``try``/``finally`` run body) can't wrap their
    whole scope in a context manager without a large re-indent. This binds the
    merged context and returns the reset token; pair it with
    :func:`reset_run_log_context` in a ``finally``. ``None`` values are dropped.
    """
    merged = dict(_run_context.get())
    for key, value in fields.items():
        if value is None:
            continue
        merged[key] = str(value)
    return _run_context.set(merged)


def reset_run_log_context(token: "contextvars.Token[dict[str, str]]") -> None:
    """Restore the correlation context captured by :func:`bind_run_log_context`."""
    _run_context.reset(token)


class ContextFilter(logging.Filter):
    """Stamp the bound correlation context onto every record.

    Implemented as a filter (not a formatter) so the bound ids are available to
    *any* formatter / handler downstream, and so structured (``extra=``) fields
    a caller already passed are preserved.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _run_context.get()
        if ctx:
            for key, value in ctx.items():
                # Don't clobber an explicit per-call extra of the same name.
                if not hasattr(record, key):
                    setattr(record, key, value)
        return True


class JsonLogFormatter(logging.Formatter):
    """Render a :class:`logging.LogRecord` as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Correlation ids (set on the record by ContextFilter) + any other
        # caller-supplied structured extras.
        for key, value in record.__dict__.items():
            if key in _RESERVED or key in payload:
                continue
            if key.startswith("_"):
                continue
            payload[key] = _coerce(value)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, separators=(",", ":"), default=str)


class TextContextFormatter(logging.Formatter):
    """Human format that appends bound correlation ids to the line.

    The ``LEGBA_LOG_FORMAT=text`` escape hatch — keeps the old readable shape
    for ``docker logs`` while still surfacing the run/correlation ids.
    """

    _BASE = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    def __init__(self) -> None:
        super().__init__(self._BASE)

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        ctx = {
            k: getattr(record, k)
            for k in ("correlation_id", "run_id", "analyst_id", "target_id")
            if hasattr(record, k)
        }
        if ctx:
            ctx_str = " ".join(f"{k}={v}" for k, v in ctx.items())
            return f"{base} [{ctx_str}]"
        return base


def _coerce(value: Any) -> Any:
    """Best-effort JSON-friendly coercion for structured extras."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def configure_structured_logging(*, level: str | None = None) -> None:
    """Install the structured logging handler on the root logger.

    Idempotent: replaces the handlers on the root logger (so a second call,
    or a prior ``basicConfig``, does not double-emit). JSON by default;
    ``LEGBA_LOG_FORMAT=text`` selects the human format. Level resolves from the
    ``level`` arg, then ``LEGBA_LOG_LEVEL``, then ``INFO``.
    """
    resolved_level = (level or os.getenv("LEGBA_LOG_LEVEL", "INFO")).upper()
    fmt = os.getenv("LEGBA_LOG_FORMAT", "json").strip().lower()
    formatter: logging.Formatter = (
        TextContextFormatter() if fmt == "text" else JsonLogFormatter()
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(ContextFilter())

    root = logging.getLogger()
    root.setLevel(getattr(logging, resolved_level, logging.INFO))
    # Drop any handlers a prior basicConfig / re-entry installed so we emit once.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
