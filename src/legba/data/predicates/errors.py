# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed exceptions for the predicate DSL pipeline.

The pipeline raises three distinct error classes so callers can route
failures correctly:

  * ``PredicateCompilationError`` — registration-time. Surface to the
    descriptor registry; the descriptor enters ``draft`` per L-104 §6.
  * ``PredicateBudgetExceeded`` — runtime hot-path. Caller treats as
    ``False`` and routes to the ``predicate_runtime_failed`` dead-letter
    per L-104 §6.
  * ``PredicateRuntimeError`` — runtime helper raised / other host
    boundary failure. Same dead-letter route as the budget exception.

All three subclass a single base so callers can catch by intent.
"""

from __future__ import annotations


class PredicateError(Exception):
    """Base class for any predicate pipeline failure."""


class PredicateCompilationError(PredicateError):
    """Predicate source failed to parse / resolve at compile time.

    Carries a clear message (file line/col when the Starlark resolver
    surfaces them) so descriptor authors can fix the source. Routed to
    the ``descriptor_validation_failed`` topic per L-104 §6.
    """

    def __init__(
        self,
        message: str,
        *,
        surface: str | None = None,
        field: str | None = None,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        super().__init__(message)
        self.surface = surface
        self.field = field
        self.line = line
        self.column = column


class PredicateBudgetExceeded(PredicateError):
    """A predicate evaluation exceeded its wall-clock / steps / memory cap.

    Per spec §3: caller treats as predicate-false, logs to dead-letter,
    pauses the descriptor after 10 consecutive failures.
    """

    def __init__(
        self,
        message: str,
        *,
        limit: str,
        budget_value: float | int,
        observed_value: float | int | None = None,
    ) -> None:
        super().__init__(message)
        self.limit = limit  # "wall_clock_ms" | "steps" | "memory_bytes"
        self.budget_value = budget_value
        self.observed_value = observed_value


class PredicateRuntimeError(PredicateError):
    """Host-boundary or helper-raised error during evaluation.

    Wraps the underlying error for observability while presenting a clean
    type at the call site. Caller treats as predicate-false.
    """

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.__cause__ = cause
