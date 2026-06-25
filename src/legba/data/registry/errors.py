# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed exceptions for the descriptor registry."""

from __future__ import annotations

from typing import Any


class RegistryError(Exception):
    """Base class for descriptor-registry exceptions."""


class DescriptorValidationError(RegistryError):
    """Raised when a descriptor fails schema or vocabulary validation.

    Carries the raw payload + structured error context so the caller (or the
    dead-letter writer) can preserve it. After a failed `register` the
    payload is routed to `descriptor_dead_letter`; the exception still
    propagates so callers know the mutation didn't land.
    """

    def __init__(
        self,
        message: str,
        *,
        attempted_payload: dict[str, Any],
        declared_schema_uri: str | None,
        validation_error: Any,
        dead_letter_id: str | None = None,
    ):
        super().__init__(message)
        self.attempted_payload = attempted_payload
        self.declared_schema_uri = declared_schema_uri
        self.validation_error = validation_error
        self.dead_letter_id = dead_letter_id


class UnknownVocabularyValue(ValueError):
    """Raised by the vocabulary validator when a descriptor references
    `entity_class` / `relationship_type` / ... values not in the live
    registry. Caught and rewrapped as DescriptorValidationError.
    """

    def __init__(self, family: str, unknown: list[str]):
        super().__init__(
            f"unknown {family} values: {sorted(unknown)}; "
            f"register via /vocabulary endpoint first"
        )
        self.family = family
        self.unknown = sorted(unknown)


class DescriptorNotFound(RegistryError):
    """Raised when a descriptor (or a specific version of one) is not found."""

    def __init__(self, family: str, descriptor_id: str, version: str | None = None):
        suffix = f" version={version}" if version else ""
        super().__init__(f"{family} descriptor {descriptor_id!r} not found{suffix}")
        self.family = family
        self.descriptor_id = descriptor_id
        self.version = version


class IllegalLifecycleTransition(RegistryError):
    """Raised when a requested state transition is not in
    `ALLOWED_TRANSITIONS` (per L-101 §6)."""

    def __init__(self, from_state: str, to_state: str):
        super().__init__(f"illegal transition {from_state} -> {to_state}")
        self.from_state = from_state
        self.to_state = to_state


class VersionConflict(RegistryError):
    """Raised when promote/rollback targets a version that does not exist
    or is otherwise inconsistent (e.g., already the head)."""


class AuditChainError(RegistryError):
    """Raised on a signing failure when writing the audit log."""
