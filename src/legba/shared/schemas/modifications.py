"""
Self-modification schemas.

Tracks proposals, snapshots, and rollbacks for agent code changes.
Adapted from AXIS shared/schemas/modifications.py, simplified for single-agent.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ModificationType(str, Enum):
    CODE = "code"
    PROMPT = "prompt"
    TOOL = "tool"
    CONFIG = "config"


class ModificationStatus(str, Enum):
    PROPOSED = "proposed"
    APPLIED = "applied"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class CodeSnapshot(BaseModel):
    """Snapshot of a file before/after modification for rollback."""

    id: UUID = Field(default_factory=uuid4)
    file_path: str
    content: str
    content_hash: str
    line_count: int
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def capture(cls, file_path: str, content: str) -> CodeSnapshot:
        return cls(
            file_path=file_path,
            content=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            line_count=content.count("\n") + 1,
        )


class ModificationProposal(BaseModel):
    """A proposed self-modification."""

    id: UUID = Field(default_factory=uuid4)
    modification_type: ModificationType
    file_path: str
    rationale: str
    expected_outcome: str
    risk_assessment: str = ""
    rollback_plan: str = ""

    # Content
    new_content: str | None = None
    diff: str | None = None

    # Related
    goal_id: UUID | None = None  # Goal that triggered this
    cycle_number: int = 0

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ModificationRecord(BaseModel):
    """Complete record of an applied modification."""

    id: UUID = Field(default_factory=uuid4)
    proposal_id: UUID
    modification_type: ModificationType
    file_path: str
    status: ModificationStatus = ModificationStatus.APPLIED

    # Snapshots
    before_snapshot: CodeSnapshot | None = None
    after_snapshot: CodeSnapshot | None = None

    # Justification
    rationale: str
    expected_outcome: str

    # Execution
    applied_at: datetime | None = None
    error: str | None = None

    # Rollback
    rolled_back_at: datetime | None = None
    rollback_reason: str | None = None

    # Context
    cycle_number: int = 0
    goal_id: UUID | None = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RollbackResult(BaseModel):
    """Result of a rollback operation."""

    success: bool
    modification_id: UUID
    rolled_back_records: list[UUID] = Field(default_factory=list)
    error: str | None = None
    rolled_back_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
