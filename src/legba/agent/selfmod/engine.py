"""
Self-Modification Engine

Handles proposing, applying, and tracking modifications to the agent's
own code, prompts, tools, and configuration.

All modifications are logged and snapshotted for rollback.
Git auto-commits every change to /agent for full history.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from ...shared.schemas.modifications import (
    CodeSnapshot,
    ModificationProposal,
    ModificationRecord,
    ModificationStatus,
    ModificationType,
    RollbackResult,
)
from ..log import CycleLogger


class SelfModEngine:
    """
    Manages self-modifications to the agent codebase.

    Modifications are:
    1. Proposed (with rationale and before-snapshot)
    2. Applied (file written, after-snapshot taken)
    3. Logged (full record stored)
    4. Git-committed (for history and rollback)

    Changes take effect next cycle when the supervisor restarts the agent.
    """

    def __init__(
        self,
        agent_code_path: str = "/agent",
        logger: CycleLogger | None = None,
    ):
        self._agent_path = Path(agent_code_path)
        self._logger = logger
        self._modifications: list[ModificationRecord] = []
        self._git_initialized = False

    async def initialize(self) -> None:
        """Initialize git repo on /agent if not already done."""
        git_dir = self._agent_path / ".git"
        if not git_dir.exists() and self._agent_path.exists():
            await self._run_git("init")
            await self._run_git("config", "user.email", "legba@agent")
            await self._run_git("config", "user.name", "Legba Agent")
            await self._run_git("add", "-A")
            await self._run_git("commit", "-m", "Initial agent code state",
                                "--allow-empty")
            self._git_initialized = True
        elif git_dir.exists():
            # Ensure identity is configured (may be missing on existing repos)
            await self._run_git("config", "user.email", "legba@agent")
            await self._run_git("config", "user.name", "Legba Agent")
            self._git_initialized = True

    async def propose_and_apply(
        self,
        file_path: str,
        new_content: str,
        rationale: str,
        expected_outcome: str,
        modification_type: ModificationType = ModificationType.CODE,
        cycle_number: int = 0,
        goal_id: UUID | None = None,
    ) -> ModificationRecord:
        """
        Propose and immediately apply a modification.

        No external review gate — the agent decides. But everything is
        logged, snapshotted, and git-committed for accountability.
        """
        full_path = self._agent_path / file_path

        # Create proposal
        proposal = ModificationProposal(
            modification_type=modification_type,
            file_path=file_path,
            rationale=rationale,
            expected_outcome=expected_outcome,
            new_content=new_content,
            cycle_number=cycle_number,
            goal_id=goal_id,
        )

        if self._logger:
            self._logger.log_self_mod("propose", file_path,
                                       rationale=rationale,
                                       proposal_id=str(proposal.id))

        # Capture before-snapshot
        before_snapshot = None
        if full_path.exists():
            before_content = full_path.read_text()
            before_snapshot = CodeSnapshot.capture(file_path, before_content)

        # Apply the modification
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(new_content)

            after_snapshot = CodeSnapshot.capture(file_path, new_content)

            record = ModificationRecord(
                proposal_id=proposal.id,
                modification_type=modification_type,
                file_path=file_path,
                status=ModificationStatus.APPLIED,
                before_snapshot=before_snapshot,
                after_snapshot=after_snapshot,
                rationale=rationale,
                expected_outcome=expected_outcome,
                applied_at=datetime.now(timezone.utc),
                cycle_number=cycle_number,
                goal_id=goal_id,
            )

            # Git commit
            if self._git_initialized:
                await self._run_git("add", file_path)
                commit_msg = f"[cycle {cycle_number}] {modification_type.value}: {rationale[:100]}"
                await self._run_git("commit", "-m", commit_msg)

            self._modifications.append(record)

            if self._logger:
                self._logger.log_self_mod("apply", file_path,
                                           record_id=str(record.id),
                                           before_hash=before_snapshot.content_hash if before_snapshot else None,
                                           after_hash=after_snapshot.content_hash)

            return record

        except Exception as e:
            record = ModificationRecord(
                proposal_id=proposal.id,
                modification_type=modification_type,
                file_path=file_path,
                status=ModificationStatus.FAILED,
                before_snapshot=before_snapshot,
                rationale=rationale,
                expected_outcome=expected_outcome,
                error=str(e),
                cycle_number=cycle_number,
                goal_id=goal_id,
            )
            self._modifications.append(record)

            if self._logger:
                self._logger.log_error(f"Self-mod failed: {e}", file_path=file_path)

            return record

    async def rollback_last(self) -> RollbackResult | None:
        """Rollback the most recent modification."""
        if not self._modifications:
            return None

        last = self._modifications[-1]
        if last.status != ModificationStatus.APPLIED:
            return None

        if last.before_snapshot is None:
            # File didn't exist before — delete it
            full_path = self._agent_path / last.file_path
            if full_path.exists():
                full_path.unlink()
        else:
            # Restore before-snapshot
            full_path = self._agent_path / last.file_path
            full_path.write_text(last.before_snapshot.content)

        last.status = ModificationStatus.ROLLED_BACK
        last.rolled_back_at = datetime.now(timezone.utc)
        last.rollback_reason = "manual rollback"

        # Git commit the rollback
        if self._git_initialized:
            await self._run_git("add", last.file_path)
            await self._run_git("commit", "-m",
                                f"[rollback] Reverted {last.file_path}")

        if self._logger:
            self._logger.log_self_mod("rollback", last.file_path,
                                       record_id=str(last.id))

        return RollbackResult(
            success=True,
            modification_id=last.id,
            rolled_back_records=[last.id],
        )

    @property
    def modifications_this_cycle(self) -> list[ModificationRecord]:
        return list(self._modifications)

    async def _run_git(self, *args: str) -> str:
        """Run a git command in the agent code directory."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", *args,
                cwd=str(self._agent_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            return stdout.decode("utf-8", errors="replace")
        except Exception:
            return ""
