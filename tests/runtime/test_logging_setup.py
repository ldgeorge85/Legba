# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resilience-observability W-1b §3 — structured JSON logging + run-context.

Exercises ``legba.runtime.logging_setup``:

  * the JSON formatter emits one parseable object per record with the standard
    fields (ts / level / logger / message);
  * ``bind_run_context`` / ``bind_run_log_context`` thread the correlation ids
    onto every record emitted within the bound scope, and unbind cleanly;
  * an explicit ``extra=`` field survives, and an exception is captured;
  * the ``LEGBA_LOG_FORMAT=text`` escape hatch appends the ids to a human line.

Pure stdlib — no substrate. Each test installs its own handler on a private
logger so the global root configuration is never mutated.
"""

from __future__ import annotations

import json
import logging

import pytest

from legba.runtime.logging_setup import (
    ContextFilter,
    JsonLogFormatter,
    TextContextFormatter,
    bind_run_context,
    bind_run_log_context,
    current_run_context,
    reset_run_log_context,
)


def _capturing_logger(name: str, formatter: logging.Formatter):
    """A throwaway logger wired to a list-capturing handler + the ContextFilter."""
    lines: list[str] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    handler = _ListHandler()
    handler.setFormatter(formatter)
    handler.addFilter(ContextFilter())
    log = logging.getLogger(name)
    log.handlers = [handler]
    log.setLevel(logging.DEBUG)
    log.propagate = False
    return log, lines


def test_json_record_has_standard_fields() -> None:
    log, lines = _capturing_logger("legba.test.json.basic", JsonLogFormatter())
    log.info("hello world")
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["level"] == "INFO"
    assert rec["logger"] == "legba.test.json.basic"
    assert rec["message"] == "hello world"
    assert "ts" in rec
    # No context bound → no run_id leaks in.
    assert "run_id" not in rec


def test_bound_context_threads_into_every_record() -> None:
    log, lines = _capturing_logger("legba.test.json.ctx", JsonLogFormatter())
    token = bind_run_log_context(run_id="abc-123", analyst_id="country_assessor")
    try:
        log.info("inside run", extra={"target_id": "brazil"})
    finally:
        reset_run_log_context(token)
    log.info("outside run")

    inside = json.loads(lines[0])
    outside = json.loads(lines[1])
    assert inside["run_id"] == "abc-123"
    assert inside["analyst_id"] == "country_assessor"
    assert inside["target_id"] == "brazil"  # explicit extra survives
    # After reset the ids are gone — no cross-run contamination.
    assert "run_id" not in outside
    assert current_run_context() == {}


def test_none_valued_context_fields_are_dropped() -> None:
    log, lines = _capturing_logger("legba.test.json.none", JsonLogFormatter())
    with bind_run_context(run_id="r1", correlation_id=None):
        log.info("only run_id")
    rec = json.loads(lines[0])
    assert rec["run_id"] == "r1"
    assert "correlation_id" not in rec


def test_exception_is_captured() -> None:
    log, lines = _capturing_logger("legba.test.json.exc", JsonLogFormatter())
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("crashed")
    rec = json.loads(lines[0])
    assert "ValueError: boom" in rec["exc_info"]


def test_text_format_appends_context() -> None:
    log, lines = _capturing_logger("legba.test.text", TextContextFormatter())
    with bind_run_context(run_id="abc", analyst_id="ca"):
        log.warning("textual line")
    assert "textual line" in lines[0]
    assert "run_id=abc" in lines[0]
    assert "analyst_id=ca" in lines[0]


def test_nested_bind_merges_then_restores() -> None:
    with bind_run_context(run_id="outer"):
        assert current_run_context()["run_id"] == "outer"
        with bind_run_context(correlation_id="inner"):
            ctx = current_run_context()
            assert ctx["run_id"] == "outer"  # outer field preserved
            assert ctx["correlation_id"] == "inner"
        # Inner scope unbound; outer survives.
        assert "correlation_id" not in current_run_context()
    assert current_run_context() == {}
